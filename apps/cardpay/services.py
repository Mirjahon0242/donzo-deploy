"""
Card payment core services (DONZO).

The Telethon user client hands every bank-notification message to
`consume_payment_message()`, which:
  1. dedups by (chat_id, message_id) — one message is consumed once;
  2. parses candidate amounts;
  3. matches an ACTIVE pending CardTopupRequest whose unique_amount equals
     a candidate;
  4. routes it:
       • unique_amount ≤ suspicious_limit → credit balance atomically
       • unique_amount  >  suspicious_limit → SuspiciousPayment (manual
         admin approve before the balance moves)
  5. writes a report to the report group (via the bot) and notifies the
     customer on Telegram.

SECURITY:
  • Balance credit happens in ONE transaction with SELECT FOR UPDATE on
    the request, its BalanceTransaction and the user — concurrent messages
    can never double-credit.
  • Suspicious transfers are NEVER auto-credited.
  • No amount from the client is ever trusted — the unique_amount is
    generated server-side and matched against the DB.
"""
import logging
import random
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.settings_app.models import Setting
from .models import CardPaymentMessage, CardTopupRequest, SuspiciousPayment, parse_amounts_from_text

logger = logging.getLogger(__name__)

# ── Setting keys (admin panel → Kalitlar / To'lov nazorati) ──
K_MONITOR_CHAT = 'payment_monitor_chat_id'
K_REPORT_CHAT = 'payment_report_chat_id'
K_LIMIT = 'payment_suspicious_limit'
K_TIMEOUT = 'payment_timeout_minutes'
K_OFFSET = 'payment_unique_offset_max'
K_CARD_NUMBER = 'payment_card_number'
K_CARD_HOLDER = 'payment_card_holder'
K_ENABLED = 'payment_card_monitor_enabled'


def get_settings() -> dict:
    """Current card-payment settings (DB-backed, admin-editable)."""
    def _int(key, default):
        try:
            return int(Setting.get_setting(key, default) or default)
        except (TypeError, ValueError):
            return default

    limit = _int(K_LIMIT, 500_000)
    timeout_min = _int(K_TIMEOUT, 10)
    offset_max = _int(K_OFFSET, 999)
    if offset_max < 0:
        offset_max = 0
    if limit < 0:
        limit = 500_000
    if timeout_min <= 0:
        timeout_min = 10

    # Card info now comes from the PaymentCard registry (auto-rotation).
    # Falls back to the legacy single-card settings while no card exists.
    card = get_active_card()
    if card is not None:
        card_number = card.card_number
        card_holder = card.card_holder
    else:
        card_number = (Setting.get_setting(K_CARD_NUMBER, '') or '').strip()
        card_holder = (Setting.get_setting(K_CARD_HOLDER, '') or '').strip()

    return {
        'monitor_chat_id': (Setting.get_setting(K_MONITOR_CHAT, '') or '').strip(),
        'report_chat_id': (Setting.get_setting(K_REPORT_CHAT, '') or '').strip(),
        'suspicious_limit': limit,
        'timeout_minutes': timeout_min,
        'offset_max': offset_max,
        'card_number': card_number,
        'card_holder': card_holder,
        'enabled': (Setting.get_setting(K_ENABLED, 'False') or '').lower() == 'true',
    }


def _sweep_daily_resets():
    """Reset counters of every card whose daily period rolled over.

    Cheap: one query filtered by period_started_at date. Ensures an
    exhausted card becomes eligible again the next morning without waiting
    to be evaluated during rotation.
    """
    from .models import PaymentCard
    today = timezone.now().date()
    stale = PaymentCard.objects.filter(auto_reset_daily=True).exclude(period_started_at__date=today)
    updated = stale.update(total_amount=0, transfers_count=0, period_started_at=timezone.now())
    if updated:
        logger.info('Daily card counter reset applied to %d card(s)', updated)


def get_active_card():
    """The currently active enabled card (or None).

    Lazily seeds a PaymentCard from the legacy single-card settings so a
    store that only ever set payment_card_number still works and gains
    rotation support.
    """
    from .models import PaymentCard
    _sweep_daily_resets()
    card = PaymentCard.objects.filter(is_active=True, enabled=True).order_by('order_index', 'id').first()
    if card is not None:
        return card
    # No active card yet — seed one from legacy settings if configured.
    legacy_number = (Setting.get_setting(K_CARD_NUMBER, '') or '').strip()
    if not legacy_number:
        return None
    legacy_holder = (Setting.get_setting(K_CARD_HOLDER, '') or '').strip()
    try:
        card, created = PaymentCard.objects.get_or_create(
            card_number=legacy_number,
            defaults={'card_holder': legacy_holder, 'is_active': True},
        )
        if created:
            logger.info('Seeded legacy card %s into PaymentCard registry', card.card_tail)
        return card
    except Exception:
        logger.exception('Failed to seed legacy card')
        return None


def match_card_by_tail(text: str):
    """Find the enabled card whose tail appears in the bank message.

    Bank messages usually include the receiving card as '💳 ***2917' or
    '**** 2917'. Matching by the last 4 digits lets the system attribute
    every transfer to the right card (and rotate precisely when that card
    hits its limit). Returns the PaymentCard or None.
    """
    from .models import PaymentCard
    if not text:
        return None
    for card in PaymentCard.objects.filter(enabled=True):
        tail = card.card_tail
        if not tail:
            continue
        # Accept both '2917' and '***2917' / '*2917' / '****2917' forms.
        if tail in text or f'*{tail}' in text or f'**{tail}' in text or f'***{tail}' in text:
            return card
    return None


def _maybe_reset_period(card) -> bool:
    """Daily auto-reset: restart counters at midnight. Returns True if reset."""
    if not card.auto_reset_daily:
        return False
    now = timezone.now()
    if card.period_started_at.date() < now.date():
        card.total_amount = 0
        card.transfers_count = 0
        card.period_started_at = now
        card.save(update_fields=['total_amount', 'transfers_count', 'period_started_at', 'updated_at'])
        return True
    return False


def register_card_payment(text: str, amount, card=None) -> dict:
    """Count a received transfer on the card it landed on.

    When the card reaches its amount/transfer limit the active card is
    automatically rotated to the next enabled one. Returns
    {card_tail, rotated: bool, new_card_tail?}.
    """
    from .models import PaymentCard
    _sweep_daily_resets()
    card = card or match_card_by_tail(text or '')
    if card is None:
        card = get_active_card()
    if card is None:
        return {'card_tail': None, 'rotated': False}

    try:
        _maybe_reset_period(card)
        card.total_amount = (card.total_amount or 0) + amount
        card.transfers_count = (card.transfers_count or 0) + 1
        card.save(update_fields=['total_amount', 'transfers_count', 'updated_at'])
    except Exception:
        logger.exception('Failed to count card usage')
        return {'card_tail': card.card_tail, 'rotated': False}

    if not card.is_exhausted:
        return {'card_tail': card.card_tail, 'rotated': False}

    # Limit reached → rotate to the next enabled card.
    result = rotate_active_card(exclude=card)
    result['card_tail'] = card.card_tail
    result['exhausted_card'] = card.card_tail
    return result


def rotate_active_card(exclude=None) -> dict:
    """Activate the next enabled card (by order_index) that isn't exhausted.

    If every card is exhausted, keeps the current one active (money must
    keep flowing; the admin is alerted via the report). Returns
    {rotated: bool, new_card_tail?}.
    """
    from .models import PaymentCard
    current = PaymentCard.objects.filter(is_active=True).first()
    if current is None:
        # Nothing active — just activate the first enabled card.
        first = PaymentCard.objects.filter(enabled=True).order_by('order_index', 'id').first()
        if first is None:
            return {'rotated': False}
        first.is_active = True
        first.save(update_fields=['is_active'])
        return {'rotated': True, 'new_card_tail': first.card_tail}

    if exclude is not None and exclude.pk == current.pk:
        pass  # rotating away from the exhausted card

    candidates = list(
        PaymentCard.objects.filter(enabled=True).exclude(pk=current.pk)
        .order_by('order_index', 'id')
    )
    for card in candidates:
        _maybe_reset_period(card)
        if not card.is_exhausted:
            current.is_active = False
            current.save(update_fields=['is_active', 'updated_at'])
            card.is_active = True
            card.last_switch_at = timezone.now()
            card.save(update_fields=['is_active', 'last_switch_at', 'updated_at'])
            _audit(
                'card_rotated', None,
                f"Karta limitga yetdi → ***{current.card_tail} dan ***{card.card_tail} ga avtomatik almashtirildi",
            )
            try:
                _send_report(
                    f"🔄 <b>DONZO | KARTA ALMASHTIRILDI</b>\n\n"
                    f"***{current.card_tail} limitga yetdi "
                    f"(summa: {current.total_amount:,.0f} so'm, o'tkazmalar: {current.transfers_count}).\n"
                    f"Yangi faol karta: <b>***{card.card_tail}</b> ({card.card_holder or '—'}).\n"
                    f"Endi mijozlarga shu karta ko'rsatiladi."
                )
            except Exception:
                logger.exception('rotation report failed')
            return {'rotated': True, 'new_card_tail': card.card_tail}

    # All other cards exhausted too — keep the current one and warn staff.
    try:
        _send_report(
            f"⚠️ <b>DONZO | BARCHA KARTALAR LIMITDA</b>\n\n"
            f"Barcha faol kartalar limitga yetdi — hozircha ***{current.card_tail} faol.\n"
            f"Admin panel → Kartalar bo'limida limitlarni oshiring yoki yangi karta qo'shing."
        )
    except Exception:
        logger.exception('all-exhausted report failed')
    return {'rotated': False}



def generate_unique_amount(requested: Decimal, offset_max: int) -> Decimal:
    """requested + random(0..offset_max) — the exact amount the user sends.

    The offset is the *identification* channel: two customers who pick the
    same nominal amount get different amounts to send, so an incoming
    transfer unambiguously maps to exactly one pending request.
    """
    if offset_max <= 0:
        return requested
    return requested + Decimal(random.randint(0, offset_max))


def create_topup_request(user, balance_tx, requested_amount: Decimal, timeout_minutes: int = None,
                         offset_max: int = None) -> CardTopupRequest:
    """Create a pending CardTopupRequest with a unique amount and expiry."""
    s = get_settings()
    timeout_minutes = timeout_minutes or s['timeout_minutes']
    offset_max = s['offset_max'] if offset_max is None else offset_max

    # Collision guard: never hand two pending requests the same unique
    # amount — a duplicate would make the matcher credit the WRONG user
    # (it picks the oldest pending request with that amount). If the offset
    # space is exhausted (e.g. offset 0, or hundreds of identical pending
    # requests for one nominal amount), raise so the caller falls back to
    # admin approval instead of risking a wrong-user credit.
    unique = None
    for _ in range(50):
        candidate = generate_unique_amount(requested_amount, offset_max)
        if not CardTopupRequest.objects.filter(
            unique_amount=candidate, status='pending',
        ).exists():
            unique = candidate
            break
    if unique is None:
        raise ValueError(
            f"unique amount space exhausted for {requested_amount} (offset {offset_max})"
        )

    return CardTopupRequest.objects.create(
        user=user,
        balance_tx=balance_tx,
        requested_amount=requested_amount,
        unique_amount=unique,
        expires_at=timezone.now() + timedelta(minutes=timeout_minutes),
        status='pending',
    )


# ────────────────────────────────────────────────────────────────────────────
# Message consumption (called by the Telethon user client)
# ────────────────────────────────────────────────────────────────────────────

def consume_payment_message(chat_id, message_id, text, sender_id=None) -> dict:
    """Handle one bank-notification message. Idempotent by (chat_id, message_id).

    Returns a dict describing the outcome:
        {ok, outcome: 'matched'|'suspicious'|'no_match'|'duplicate'|'expired_late'|'disabled',
         amount?, request_id?, suspicious_id?, credit_amount?}
    """
    s = get_settings()
    if not s['enabled']:
        return {'ok': False, 'outcome': 'disabled'}

    # 1) Dedup — create the message row inside its own savepoint so an
    #    IntegrityError (duplicate chat+message) can never poison the outer
    #    transaction: we catch it, roll back the savepoint, and return.
    try:
        with transaction.atomic():
            msg = CardPaymentMessage.objects.create(
                chat_id=str(chat_id),
                message_id=int(message_id),
                raw_text=(text or '')[:2000],
                parsed_amounts=','.join(str(a) for a in parse_amounts_from_text(text or '')),
                sender_id=str(sender_id) if sender_id else None,
            )
    except Exception:
        # IntegrityError → already consumed. Return without side effects.
        return {'ok': False, 'outcome': 'duplicate'}

    amounts = parse_amounts_from_text(text or '')
    if not amounts:
        msg.outcome = 'no_match'
        msg.save(update_fields=['outcome'])
        return {'ok': False, 'outcome': 'no_match'}

    # 2) Find the pending request whose unique_amount is among the candidates.
    req = None
    for a in amounts:
        req = CardTopupRequest.objects.filter(
            unique_amount=Decimal(a), status='pending',
        ).order_by('created_at').first()
        if req:
            break

    if req is None:
        msg.outcome = 'no_match'
        msg.save(update_fields=['outcome'])
        return {'ok': False, 'outcome': 'no_match', 'amounts': amounts}

    amount = req.unique_amount

    # 3) Route: suspicious vs normal
    if amount > Decimal(s['suspicious_limit']):
        sp = SuspiciousPayment.objects.create(
            message=msg,
            user=req.user,
            amount=amount,
            topup_request=req,
            status='pending',
            note=f"Kutilgan {req.unique_amount} so'm — shubhali limitdan ({s['suspicious_limit']}) yuqori",
        )
        msg.outcome = 'suspicious'
        msg.save(update_fields=['outcome'])
        _send_report(_suspicious_report_text(sp, req, s))
        # Extra staff alert: every admin/operator Telegram chat gets a direct
        # notification with Approve/Reject buttons (hisobot guruhidan tashqari).
        try:
            from apps.users.telegram_notify import notify_staff_suspicious_payment
            notify_staff_suspicious_payment(sp)
        except Exception:
            logger.exception('staff suspicious notify failed')
        return {'ok': True, 'outcome': 'suspicious', 'amount': str(amount),
                'suspicious_id': sp.id, 'request_id': req.id}

    # 4) Normal — run the SECURITY engine first (rules + Gemini AI).
    #    Only APPROVED payments get credited; HOLD/MANUAL_REVIEW/BLOCKED
    #    create an incident + alert and never move the balance.
    #    In shadow mode the AI's opinion is recorded but LOW/MEDIUM rule
    #    levels still proceed (AI observes, never enforces).
    try:
        from apps.security import services as sec_services
        sec = sec_services.evaluate_payment(req.user, amount, req, message=msg)
    except Exception as exc:
        # Fail-open only when configured (security_fail_open=True). Otherwise
        # a security-layer error must NEVER silently credit money — hold it
        # for manual review.
        logger.exception('Security evaluation failed')
        from apps.security.risk_engine import get_security_settings as _sec_settings
        if _sec_settings()['fail_open']:
            sec = {'decision': 'APPROVED', 'level': 'LOW', 'final_score': 0,
                   'reasons': [f'Security tahlili xatosi — fail_open ({type(exc).__name__})']}
        else:
            sec = {'decision': 'HOLD', 'level': 'HIGH', 'final_score': 100,
                   'reasons': [f'Security tahlili xatosi ({type(exc).__name__}) — ehtiyot chorasi HOLD']}

    if sec.get('decision') in ('HOLD', 'MANUAL_REVIEW', 'BLOCKED'):
        msg.outcome = 'held'
        msg.save(update_fields=['outcome'])
        try:
            from apps.security.alerts import notify_incident
            from apps.security.models import SecurityIncident
            if sec.get('incident_id'):
                incident = SecurityIncident.objects.get(pk=sec['incident_id'])
                notify_incident(incident)
        except Exception:
            logger.exception('incident notify failed')
        _send_report(
            f"🛡️ <b>DONZO | TO'LOV USHLAB TURILDI</b>\n\n"
            f"Summa: <b>{amount:,.0f}</b> so'm\n"
            f"Risk: <b>{sec.get('level', '?')}</b> ({sec.get('final_score', '?')}/100)\n"
            f"User: @{req.user.telegram_username or req.user.username}\n"
            f"Sabablar:\n" + "\n".join(f"• {r}" for r in (sec.get('reasons') or [])[:6]) + "\n\n"
            f"Admin panel → Xavfsizlik markazi → Incident #{sec.get('incident_id')}"
        )
        return {'ok': False, 'outcome': 'held', 'amount': str(amount),
                'request_id': req.id, 'incident_id': sec.get('incident_id'),
                'risk_level': sec.get('level'), 'risk_score': sec.get('final_score')}

    # 5) Approved — credit the balance atomically.
    result = credit_request(req, matched_message=msg)
    if result.get('credited'):
        msg.outcome = 'matched'
        msg.save(update_fields=['outcome'])
        _send_report(_paid_report_text(req, s, received=amount))
        _notify_user_paid(req.user, amount)
        # Count the transfer on the card it landed on; auto-rotate when the
        # card reaches its amount/transfer limit.
        try:
            register_card_payment(text, amount)
        except Exception:
            logger.exception('card usage registration failed')
        return {'ok': True, 'outcome': 'matched', 'amount': str(amount),
                'request_id': req.id, 'credit_amount': str(result['credit_amount']),
                'risk_level': sec.get('level', 'LOW')}
    # Race: request was already handled (parallel message) or expired at the
    # exact moment the transfer arrived. Money may have reached the card but
    # was NOT credited — surface it to staff so it is never silently lost.
    msg.outcome = 'duplicate'
    msg.save(update_fields=['outcome'])
    if req.status == 'expired':
        _send_report(
            f"🕐 <b>DONZO | KECIKKEN TO'LOV</b>\n\n"
            f"Summa: <b>{amount:,.0f}</b> so'm keldi, lekin so'rov muddati o'tgan "
            f"(#{req.id}). Balansga tushmadi.\n"
            f"Vaqt: {_fmt_now()}\n\n"
            f"Admin panel → To'lov nazorati'da tekshiring."
        )
        return {'ok': False, 'outcome': 'expired_late', 'amount': str(amount), 'request_id': req.id}
    return {'ok': False, 'outcome': 'duplicate', 'amount': str(amount)}


@transaction.atomic
def credit_request(req, matched_message=None, allow_expired=False) -> dict:
    """Credit a user's balance for a matched top-up. Row-locked, idempotent.

    Credits the UNIQUE amount actually received (what the customer sent).
    Concurrency: two messages matching the same request — the second sees
    status != 'pending' and does nothing.

    allow_expired=True (admin EXPLICIT approval of a held payment whose
    request expired while they decided): the expired/cancelled request whose
    money actually arrived is reopened and credited, and marked paid so the
    ledger stays consistent. Never credits an already-paid request.
    """
    from apps.users.models import User

    req = CardTopupRequest.objects.select_for_update().get(pk=req.pk)
    if req.status == 'paid':
        return {'credited': False, 'reason': f'status={req.status}'}
    if req.status != 'pending' and not (allow_expired and req.status in ('expired', 'cancelled')):
        return {'credited': False, 'reason': f'status={req.status}'}

    user = User.objects.select_for_update().get(pk=req.user_id)
    credit = req.unique_amount

    tx = req.balance_tx
    if tx is not None:
        tx = req.balance_tx.__class__.objects.select_for_update().get(pk=tx.pk)
        if tx.status == 'completed':
            return {'credited': False, 'reason': f'tx status={tx.status}'}
        if tx.status != 'pending' and not allow_expired:
            return {'credited': False, 'reason': f'tx status={tx.status}'}
        # Accurate ledger: record the REAL credited amount and the current
        # balance snapshot (history shows +unique_amount, matching the delta).
        tx.amount = credit
        tx.balance_before = user.balance
        tx.balance_after = user.balance + credit
        tx.status = 'completed'
        tx.provider = 'card'
        tx.description = (
            "Karta to'lovi admin tomonidan tasdiqlandi (muddati o'tgan edi)"
            if allow_expired else
            "Karta to'lovi avtomatik tasdiqlandi"
        )
        tx.save(update_fields=['status', 'amount', 'balance_before', 'balance_after', 'provider', 'description'])

    user.balance += credit
    user.save(update_fields=['balance'])

    req.status = 'paid'
    req.paid_at = timezone.now()
    req.matched_message = matched_message
    req.save(update_fields=['status', 'paid_at', 'matched_message', 'updated_at'])

    _audit('card_payment_credited', req.user, f"Karta to'lovi #{req.id}: +{credit} so'm balansga tushdi")

    return {'credited': True, 'credit_amount': credit, 'balance_after': user.balance}


def approve_suspicious(sp_id, actor) -> dict:
    """Admin approves a suspicious transfer → credit the target balance.

    If the suspicious payment is tied to a pending request, the request is
    paid with the received amount; otherwise a standalone BalanceTransaction
    is created (credited directly, the owner is the suspicious.user).
    """
    from apps.users.models import User

    with transaction.atomic():
        sp = SuspiciousPayment.objects.select_for_update().get(pk=sp_id)
        if sp.status != 'pending':
            return {'ok': False, 'detail': f"Holat: {sp.status}"}

        user = User.objects.select_for_update().get(pk=sp.user_id) if sp.user_id else None
        if user is None:
            return {'ok': False, 'detail': 'Foydalanuvchi topilmadi'}

        credit = sp.amount
        req = sp.topup_request
        tx = None
        if req is not None:
            req = CardTopupRequest.objects.select_for_update().get(pk=req.pk)
            # SECURITY: never credit twice. A bank can send duplicate
            # notification messages → multiple SuspiciousPayment rows for
            # the SAME request. The first approval pays the request; a
            # second approval must NOT credit the balance again.
            if req.status == 'paid':
                return {'ok': False, 'detail': "Bu so'rov uchun balans allaqachon to'ldirilgan"}
            if req.status == 'pending':
                req.status = 'paid'
                req.paid_at = timezone.now()
                req.save(update_fields=['status', 'paid_at', 'updated_at'])
            tx = req.balance_tx

        if tx is not None:
            tx = tx.__class__.objects.select_for_update().get(pk=tx.pk)
            if tx.status == 'completed':
                return {'ok': False, 'detail': 'Tranzaksiya allaqachon tugallangan — takroriy tasdiqlash mumkin emas'}
            # Reopen even a cancelled/expired tx (the money actually arrived)
            tx.amount = credit
            tx.balance_before = user.balance
            tx.balance_after = user.balance + credit
            tx.status = 'completed'
            tx.provider = 'card'
            tx.description = "Shubhali to'lov admin tomonidan tasdiqlandi"
            tx.save(update_fields=['status', 'amount', 'balance_before', 'balance_after', 'provider', 'description'])
        else:
            from apps.payments.models import BalanceTransaction
            tx = BalanceTransaction.objects.create(
                user=user,
                tx_type='topup',
                amount=credit,
                balance_before=user.balance,
                balance_after=user.balance + credit,
                status='completed',
                provider='card',
                description="Shubhali to'lov admin tomonidan tasdiqlandi (avtomatik)",
            )

        user.balance += credit
        user.save(update_fields=['balance'])

        sp.status = 'approved'
        sp.decided_at = timezone.now()
        sp.decided_by = actor
        sp.save(update_fields=['status', 'decided_at', 'decided_by'])

    _audit('suspicious_payment_approved', actor, f"Shubhali to'lov #{sp.id}: +{credit} so'm → @{user.username}")
    _notify_user_paid(user, credit)
    _send_report(_suspicious_approved_report_text(sp, user, credit))
    # Count the transfer on the card (limits → auto-rotate).
    try:
        msg_text = sp.message.raw_text if sp.message else ''
        register_card_payment(msg_text, credit)
    except Exception:
        logger.exception('card usage registration failed (suspicious approve)')
    return {'ok': True, 'credited': True, 'credit_amount': str(credit)}


def reject_suspicious(sp_id, actor, note='') -> dict:
    """Admin rejects a suspicious transfer — nothing is credited."""
    with transaction.atomic():
        sp = SuspiciousPayment.objects.select_for_update().get(pk=sp_id)
        if sp.status != 'pending':
            return {'ok': False, 'detail': f"Holat: {sp.status}"}
        sp.status = 'rejected'
        sp.decided_at = timezone.now()
        sp.decided_by = actor
        sp.note = note or sp.note
        sp.save(update_fields=['status', 'decided_at', 'decided_by', 'note'])

    _audit('suspicious_payment_rejected', actor, f"Shubhali to'lov #{sp.id}: {sp.amount} so'm rad etildi")
    _send_report(_suspicious_rejected_report_text(sp))
    if sp.user:
        try:
            from apps.users.telegram_notify import send_to_user
            send_to_user(
                sp.user,
                f"❌ <b>To'lov rad etildi</b>\n\n"
                f"{sp.amount:,.0f} so'm miqdordagi shubhali to'lov balansga tushmadi. "
                f"Sababini bilish uchun operator bilan bog'laning.",
            )
        except Exception:
            logger.exception('suspicious reject notify failed')
    return {'ok': True}


def expire_stale_requests() -> int:
    """Cancel pending requests whose window elapsed. Returns count expired."""
    now = timezone.now()
    stale = CardTopupRequest.objects.filter(status='pending', expires_at__lt=now)
    ids = list(stale.values_list('id', flat=True))
    updated = stale.update(status='expired', updated_at=now)
    for rid in ids:
        try:
            req = CardTopupRequest.objects.select_related('balance_tx').get(pk=rid)
            # Also cancel the linked pending BalanceTransaction so the admin
            # panel shows it as cancelled (never approved later).
            tx = req.balance_tx
            if tx is not None and tx.status == 'pending':
                from apps.payments.models import BalanceTransaction
                BalanceTransaction.objects.filter(pk=tx.pk, status='pending').update(
                    status='cancelled',
                    description=f"To'lov vaqti tugadi (10 daqiqa) — request #{rid}",
                )
            _audit('card_payment_expired', req.user,
                   f"To'lov vaqti tugadi #{req.id}: {req.unique_amount} so'm")
        except Exception:
            logger.exception('audit on expiry failed')
    return updated


# ────────────────────────────────────────────────────────────────────────────
# Reports (via the bot to the report group) + status report
# ────────────────────────────────────────────────────────────────────────────

def _bot_token():
    return (Setting.get_setting('telegram_bot_token', '') or '').strip()


def _send_report(text: str) -> bool:
    """Send a report to the configured report group via the Bot API.

    Prefers the dedicated report bot (health_report_bot_token) when
    configured — that bot is usually the one added to the report group —
    and falls back to the main bot token. Never raises; a report failure
    must never break the payment flow.
    """
    try:
        from apps.users.telegram_notify import _send_message
        chat_id = get_settings()['report_chat_id']
        token = (Setting.get_setting('health_report_bot_token', '') or '').strip() or _bot_token()
        if not chat_id or not token:
            return False
        return bool(_send_message(token, chat_id, text))
    except Exception:
        logger.exception('Report send failed')
        return False


def _fmt_now():
    return timezone.now().strftime('%d.%m.%Y %H:%M')


def _paid_report_text(req, s, received) -> str:
    user = req.user
    return (
        "💰 <b>DONZO | BALANS TO'LDIRILDI</b>\n\n"
        f"User: @{user.telegram_username or user.username}\n"
        f"Telegram ID: <code>{user.telegram_id or '—'}</code>\n"
        f"So'ralgan: <b>{req.requested_amount:,.0f}</b> so'm\n"
        f"Kelgan: <b>{received:,.0f}</b> so'm\n"
        f"Balansga: <b>{received:,.0f}</b> so'm\n"
        f"Status: <b>PAID</b>\n"
        f"Vaqt: {_fmt_now()}\n"
        f"Request: <code>#{req.id}</code>\n"
        f"Limit: {s['suspicious_limit']:,} so'm"
    )


def _suspicious_report_text(sp, req, s) -> str:
    user = sp.user
    return (
        "⚠️ <b>DONZO | SHUBHALI TO'LOV</b>\n\n"
        f"User: @{user.telegram_username or user.username if user else '—'}\n"
        f"Telegram ID: <code>{user.telegram_id if user else '—'}</code>\n"
        f"Kelgan: <b>{sp.amount:,.0f}</b> so'm\n"
        f"Limit: <b>{s['suspicious_limit']:,}</b> so'm\n"
        f"Status: <b>SUSPICIOUS — balansga tushmadi</b>\n"
        f"Vaqt: {_fmt_now()}\n\n"
        f"Admin panel → To'lov nazorati → Shubhali bo'limida tasdiqlang."
    )


def _suspicious_approved_report_text(sp, user, credit) -> str:
    return (
        "✅ <b>DONZO | SHUBHALI TO'LOV TASDIQLANDI</b>\n\n"
        f"User: @{user.telegram_username or user.username}\n"
        f"Summa: <b>{credit:,.0f}</b> so'm balansga tushdi\n"
        f"Vaqt: {_fmt_now()}"
    )


def _suspicious_rejected_report_text(sp) -> str:
    return (
        "❌ <b>DONZO | SHUBHALI TO'LOV RAD ETILDI</b>\n\n"
        f"Summa: <b>{sp.amount:,.0f}</b> so'm — balansga tushmadi\n"
        f"Vaqt: {_fmt_now()}"
    )


def build_status_report() -> str:
    """Daily-status report for the Saved Messages 'status' command."""
    from django.db.models import Sum

    today = timezone.now().date()
    s = get_settings()
    paid_today = CardTopupRequest.objects.filter(
        status='paid', paid_at__date=today,
    ).count()
    total_today = CardTopupRequest.objects.filter(
        paid_at__date=today,
    ).aggregate(total=Sum('unique_amount'))['total'] or 0
    pending = CardTopupRequest.objects.filter(status='pending').count()
    suspicious_pending = SuspiciousPayment.objects.filter(status='pending').count()
    monitor_online = _user_client_online()

    return (
        "📊 <b>DONZO PAYMENT STATUS</b>\n\n"
        f"<b>Bugun:</b>\n"
        f"To'lovlar: <b>{paid_today}</b>\n"
        f"Jami: <b>{total_today:,.0f}</b> so'm\n"
        f"Pending: <b>{pending}</b>\n"
        f"Shubhali (kutilmoqda): <b>{suspicious_pending}</b>\n\n"
        f"<b>User Client:</b> {'ONLINE' if monitor_online else 'OFFLINE'}\n"
        f"Monitor chat: <code>{s['monitor_chat_id'] or '—'}</code>\n"
        f"Report group: <code>{s['report_chat_id'] or '—'}</code>\n"
        f"Limit: {s['suspicious_limit']:,} so'm | Timeout: {s['timeout_minutes']} daqiqa"
    )


def build_health_report() -> str:
    """Periodic system health report for the report group (every 15 min).

    Checks each component and lists what is down; if everything is fine the
    message says so. Sent by the user_client worker via the bot.
    """
    import json
    from datetime import datetime, timezone as dt_timezone
    from pathlib import Path
    import urllib.request

    root = Path(__file__).resolve().parents[3]  # DONZO/
    now = timezone.now()
    lines = []
    problems = []

    def _check(label, ok, detail=''):
        lines.append(f"{'✅' if ok else '❌'} <b>{label}</b>{(' — ' + str(detail)) if detail else ''}")
        if not ok:
            problems.append(label)

    # 1) Bot
    bot_ok, bot_detail = False, 'stats topilmadi'
    try:
        stats = json.loads((root / '.freebuff' / 'bot-stats.json').read_text(encoding='utf-8'))
        hb = stats.get('last_heartbeat')
        if hb:
            dt = datetime.fromisoformat(hb)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=dt_timezone.utc)
            fresh = (datetime.now(dt_timezone.utc) - dt).total_seconds() < 180
            valid = bool(stats.get('token_status', {}).get('valid'))
            bot_ok = fresh and valid
            bot_detail = 'ishlayapti' if fresh else 'heartbeat eskirgan'
            if not valid:
                bot_detail = 'token yaroqsiz'
    except Exception:
        pass
    _check('Bot (@DONZOROBOT)', bot_ok, bot_detail)

    # 2) Backend (daphne)
    # Backend http'ga 301 (https'ga) qaytaradi — urlopen redirect'ni kuzatib
    # https:// ga o'tadi, TLS yo'qligi uchun timeout bo'ladi va sog'lom
    # backend "o'lik" deb ko'rsatilardi. Redirect'ni o'chirib, 2xx/3xx ni
    # tirik deb hisoblaymiz.
    backend_ok = False
    try:
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        _opener = urllib.request.build_opener(_NoRedirect)
        with _opener.open('http://localhost:8000/health/', timeout=5) as r:
            backend_ok = r.status == 200
    except urllib.error.HTTPError as e:
        # 3xx (https'ga redirect) ham 'tirik' — backend javob berdi
        backend_ok = e.code in (301, 302, 303, 307, 308)
    except Exception:
        pass
    _check('Backend (8000)', backend_ok, '' if backend_ok else 'javob bermayapti')

    # 3) Tunnel (public API URL from frontend/.env.local)
    tunnel_ok, tunnel_url = False, ''
    try:
        env = root / 'frontend' / '.env.local'
        if env.exists():
            for line in env.read_text(encoding='utf-8', errors='replace').splitlines():
                if line.startswith('NEXT_PUBLIC_API_URL='):
                    tunnel_url = line.split('=', 1)[1].strip().strip('"').strip("'")
                    break
    except Exception:
        pass
    if tunnel_url:
        try:
            with urllib.request.urlopen(tunnel_url.rstrip('/') + '/categories/', timeout=8) as r:
                tunnel_ok = r.status == 200
        except Exception:
            pass
    _check('Tunnel', tunnel_ok, '' if tunnel_ok else ('API URL topilmadi' if not tunnel_url else 'javob bermayapti'))

    # 4) User Client (worker itself)
    uc_ok, uc_detail = False, 'stats topilmadi'
    try:
        stats = json.loads((root / '.freebuff' / 'user-client-stats.json').read_text(encoding='utf-8'))
        hb = stats.get('last_heartbeat')
        if hb:
            dt = datetime.fromisoformat(hb)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=dt_timezone.utc)
            fresh = (datetime.now(dt_timezone.utc) - dt).total_seconds() < 180
            uc_ok = fresh
            uc_detail = 'ONLINE' if fresh else 'heartbeat eskirgan'
        else:
            uc_detail = 'heartbeat yo\'q'
    except Exception:
        pass
    _check('User Client', uc_ok, uc_detail)

    # 5) Monitor chat
    s = get_settings()
    monitor_ok = bool(s.get('monitor_chat_id'))
    _check('Monitor chat', monitor_ok, s.get('monitor_chat_id') or 'sozlanmagan')

    # 6) Stats
    from django.db.models import Sum
    today = now.date()
    paid_qs = CardTopupRequest.objects.filter(status='paid', paid_at__date=today)
    paid_today = paid_qs.count()
    total_today = paid_qs.aggregate(t=Sum('unique_amount'))['t'] or 0
    pending = CardTopupRequest.objects.filter(status='pending').count()
    suspicious = SuspiciousPayment.objects.filter(status='pending').count()

    head = '✅ <b>HAMMASI ISHLAMOQDA</b>' if not problems else '⚠️ <b>MUAMMOLAR BOR</b>'
    body = '\n'.join(lines)
    stats_line = (f"📥 Kutilayotgan: <b>{pending}</b> | Shubhali: <b>{suspicious}</b> | "
                  f"Bugun to'langan: <b>{paid_today}</b> ({total_today:,.0f} so'm)")
    foot = '✅ Hammasi ishlayapti' if not problems else '❗ Tuzatish kerak: ' + ', '.join(problems)
    return (
        f"📊 <b>DONZO HOLATI</b> — {now.strftime('%d.%m %H:%M')}\n\n"
        f"{head}\n\n"
        f"{body}\n\n"
        f"{stats_line}\n\n"
        f"{foot}"
    )


def send_health_report() -> bool:
    """Build + send the periodic health report to the report group via bot."""
    try:
        return _send_report(build_health_report())
    except Exception:
        logger.exception('health report send failed')
        return False


def _user_client_online() -> bool:
    """Heartbeat check from .freebuff/user-client-stats.json."""
    import json
    from datetime import datetime
    from pathlib import Path
    try:
        stats_path = Path(__file__).resolve().parents[3] / '.freebuff' / 'user-client-stats.json'
        if not stats_path.exists():
            return False
        data = json.loads(stats_path.read_text(encoding='utf-8'))
        hb = data.get('last_heartbeat')
        if not hb:
            return False
        dt = datetime.fromisoformat(hb)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (timezone.now() - dt).total_seconds() < 180
    except Exception:
        return False


def _notify_user_paid(user, credit) -> None:
    """Fire-and-forget Telegram notification that the top-up was credited."""
    try:
        from apps.users.telegram_notify import send_to_user
        send_to_user(
            user,
            f"✅ <b>Balans to'ldirildi!</b>\n\n"
            f"Karta to'lovingiz tasdiqlandi: <b>{credit:,.0f}</b> so'm hisobingizga "
            f"qo'shildi. Joriy balans: <b>{user.balance:,.0f}</b> so'm.",
        )
    except Exception:
        logger.exception('user paid notify failed')


def _audit(action, user, description) -> None:
    try:
        from apps.audit_log.models import AuditLog
        AuditLog.objects.create(
            user=user,
            action=action,
            target_type='CardTopupRequest',
            description=description,
        )
    except Exception:
        logger.exception('audit failed')
