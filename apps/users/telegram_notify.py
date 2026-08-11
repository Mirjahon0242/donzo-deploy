"""
Telegram staff notifications on auth failures.

When a user's Telegram WebApp login is rejected (401), the platform
sends a short alert to every admin/operator whose account is linked
to a Telegram chat so staff can react (device clock skew, stale
token, repeated brute-force attempts, etc.).

SECURITY: this module never logs initData, hashes, or bot tokens.
The alert only carries the error code and a timestamp.

NOTE: the login-widget 401 path (TelegramAuthView) reports the synthetic
code `login_widget_hash_mismatch` since the widget verifier returns a bare
bool — it is not a typo, just a distinct marker from the WebApp codes.
"""
import logging
import time

from django.utils import timezone

logger = logging.getLogger(__name__)

# Roles whose linked Telegram chats receive auth-failure alerts.
NOTIFY_ROLES = ('super_admin', 'admin', 'senior_operator', 'operator', 'support')

# Max one alert per error_code per window (seconds) — prevents spam when
# a single user (or attacker) retries repeatedly.
THROTTLE_SECONDS = 300

# Outbound Telegram API timeout (seconds).
NOTIFY_TIMEOUT = 8


def _get_staff_telegram_ids():
    """Return the set of Telegram chat ids of staff roles (never customers)."""
    from .models import User
    return set(
        User.objects.filter(
            role__in=NOTIFY_ROLES,
            telegram_id__isnull=False,
        ).exclude(telegram_id='').values_list('telegram_id', flat=True)
    )


def _suspicious_buttons(suspicious_id: int) -> dict:
    """Inline Approve/Reject buttons for a suspicious card payment.

    Callback format matches bot.py's `sp:` handler so a staff member can
    act on the payment right from the notification — without opening the
    admin panel.
    """
    return {'inline_keyboard': [
        [
            {'text': '✅ Tasdiqlash', 'callback_data': f'sp:{suspicious_id}:approve'},
            {'text': '❌ Rad etish', 'callback_data': f'sp:{suspicious_id}:reject'},
        ]
    ]}


def notify_staff_suspicious_payment(suspicious) -> int:
    """
    Alert every staff Telegram chat (admin/operator/support) that a
    suspicious card payment arrived — IN ADDITION to the report group.

    The message carries inline Approve/Reject buttons wired to bot.py's
    `sp:` callback handler. Throttled per suspicious-payment id: a payment
    alerts staff exactly once. Never raises — the payment flow must never
    break or slow down because of a notification issue.

    Returns the number of chats successfully notified.
    """
    import time as _time
    try:
        if suspicious is None or suspicious.status != 'pending':
            return 0
        sp_id = suspicious.id

        # Throttle: at most one alert per suspicious payment within the
        # window. The window has a TTL so a payment left pending for hours
        # can re-alert staff (one reminder, not an endless spam loop).
        from apps.settings_app.models import Setting
        throttle_key = f'sp_alert_{sp_id}'
        SP_ALERT_TTL_SECONDS = 6 * 3600  # 6 soat — keyin qayta ogohlantirish
        last = Setting.get_setting(throttle_key, '')
        if last:
            try:
                if (_time.time() - float(last)) < SP_ALERT_TTL_SECONDS:
                    return 0
            except (TypeError, ValueError):
                return 0

        bot_token = Setting.get_setting('telegram_bot_token', '')
        if not bot_token:
            return 0

        chat_ids = _get_staff_telegram_ids()
        if not chat_ids:
            return 0

        user = suspicious.user
        username = f"@{user.telegram_username or user.username}" if user else '—'
        text = (
            f"⚠️ <b>SHUBHALI TO'LOV</b> — xodimlar uchun bildirishnoma\n\n"
            f"Summa: <b>{suspicious.amount:,.0f}</b> so'm\n"
            f"Foydalanuvchi: {username}\n"
            f"Telegram ID: <code>{user.telegram_id if user else '—'}</code>\n"
            f"Vaqt: {timezone.now().strftime('%d.%m.%Y %H:%M')}\n\n"
            f"<b>Balansga tushmadi.</b> Quyidagi tugmalar orqali qaror qiling:"
        )

        sent = 0
        for chat_id in chat_ids:
            try:
                if _send_message(bot_token, chat_id, text,
                                 reply_markup=_suspicious_buttons(sp_id)):
                    sent += 1
            except Exception:
                logger.exception(f"Failed to notify staff chat {chat_id}")

        if sent:
            Setting.set_setting(throttle_key, str(_time.time()))
            try:
                from apps.audit_log.models import AuditLog
                AuditLog.objects.create(
                    action='suspicious_payment_staff_notified',
                    target_type='SuspiciousPayment',
                    description=(
                        f"Shubhali to\'lov #{sp_id}: {suspicious.amount} so'm — "
                        f"{sent} ta xodim chatiga bildirishnoma yuborildi"
                    ),
                )
            except Exception:
                logger.exception("Failed to record suspicious-notify audit log")
        return sent
    except Exception:
        logger.exception("Failed to notify staff of suspicious payment")
        return 0


def _send_message(bot_token: str, chat_id: str, text: str, reply_markup=None) -> bool:
    """Send a text message to one chat via the Bot API. Returns success.

    Uses stdlib urllib — no extra dependency required beyond Django's stdlib.
    reply_markup: optional inline-keyboard dict exactly as the Bot API accepts
    (e.g. {'inline_keyboard': [[{'text': '...', 'web_app': {'url': '...'}}]]}).
    """
    import json
    import urllib.request
    url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': True,
    }
    if reply_markup:
        payload['reply_markup'] = reply_markup
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        url, data=data,
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=NOTIFY_TIMEOUT) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        return bool(data.get('ok'))


def _throttle_key(error_code: str) -> str:
    return f'tg_fail_notify_{error_code}'


def _throttled(error_code: str) -> bool:
    """True when this error_code was already alerted within the window."""
    from apps.settings_app.models import Setting
    last = Setting.get_setting(_throttle_key(error_code), '')
    if not last:
        return False
    try:
        last_ts = float(last)
    except (TypeError, ValueError):
        return False
    return (time.time() - last_ts) < THROTTLE_SECONDS


def notify_admins_of_auth_failure(error_code: str):
    """
    Alert staff Telegram chats that a WebApp login failed with the given
    error_code. Throttled per error_code. NEVER raises — the auth endpoint
    must never break or slow down because of a notification issue.
    """
    if not error_code:
        return
    try:
        if _throttled(error_code):
            return

        from apps.settings_app.models import Setting
        bot_token = Setting.get_setting('telegram_bot_token', '')
        if not bot_token:
            return

        chat_ids = _get_staff_telegram_ids()
        if not chat_ids:
            return

        text = (
            f"⚠️ <b>Kirishda xatolik</b>\n\n"
            f"Foydalanuvchi Telegram Web App orqali kirishda "
            f"muvaffaqiyatsizlikka uchradi.\n"
            f"<b>Error code:</b> <code>{error_code}</code>\n"
            f"<b>Vaqt:</b> {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        sent_any = False
        for chat_id in chat_ids:
            try:
                if _send_message(bot_token, chat_id, text):
                    sent_any = True
            except Exception:
                logger.exception(f"Failed to notify staff chat {chat_id}")

        # Throttle only when at least one message actually went out.
        if sent_any:
            Setting.set_setting(_throttle_key(error_code), str(time.time()))

        # Audit trail so the admin panel can show when staff was alerted.
        try:
            from apps.audit_log.models import AuditLog
            AuditLog.objects.create(
                action='tg_auth_failure_notified',
                target_type='error_code',
                description=f'Staff notified of auth failure: {error_code}',
            )
        except Exception:
            logger.exception("Failed to record auth-failure notification audit log")
    except Exception:
        logger.exception("Failed to notify staff of auth failure")


# ────────────────────────────────────────────────────────────────────────────
# Customer notifications (order status, payments, top-ups)
# ────────────────────────────────────────────────────────────────────────────
# These fire from Django request handlers (operator changes an order status,
# admin approves a top-up, a balance payment succeeds). They are deliberately
# fire-and-forget + never-raise: a slow or failed Telegram call must never
# block or break the API request that triggered it.

STATUS_EMOJI = {
    'pending': '🕐',
    'processing': '⚙️',
    'completed': '✅',
    'cancelled': '❌',
}

STATUS_LABEL = {
    'pending': 'Kutilmoqda',
    'processing': 'Bajarilmoqda',
    'completed': 'Tugallangan',
    'cancelled': 'Bekor qilingan',
}


def _order_webapp_button(order):
    """Inline 'Buyurtmani ko\'rish' web-app button, or None if no HTTPS URL."""
    from apps.settings_app.models import Setting
    web_app_url = Setting.get_setting('web_app_url', '')
    if not web_app_url or not str(web_app_url).startswith('https://'):
        return None
    base = str(web_app_url).rstrip('/')
    return {'inline_keyboard': [[
        {'text': "📦 Buyurtmani ko'rish", 'web_app': {'url': f'{base}/orders/{order.id}'}},
    ]]}


def send_to_user(user, text: str, reply_markup=None):
    """Send an HTML message to a user's linked Telegram chat. Never raises.

    The actual Bot API call runs in a daemon thread so a slow/unreachable
    Telegram endpoint can NEVER block or slow down the API request that
    triggered the notification (order status change, top-up approval, ...).
    """
    import threading
    try:
        if not user or not getattr(user, 'telegram_id', None):
            return False
        from apps.settings_app.models import Setting
        bot_token = Setting.get_setting('telegram_bot_token', '')
        if not bot_token:
            return False
        chat_id = str(user.telegram_id)

        def _deliver():
            try:
                _send_message(bot_token, chat_id, text, reply_markup=reply_markup)
            except Exception:
                logger.exception("Failed to deliver Telegram message")

        threading.Thread(target=_deliver, daemon=True).start()
        return True
    except Exception:
        logger.exception("Failed to send Telegram message to user")
        return False


def notify_order_status(order, old_status: str, new_status: str):
    """Notify the order's customer that its status changed."""
    try:
        if not order or not order.customer:
            return
        service_name = order.service.name if getattr(order, 'service', None) else 'Xizmat'
        package_name = order.package.name if getattr(order, 'package', None) else ''
        lines = [
            "📦 <b>Buyurtma holati yangilandi</b>\n",
            f"<b>#{order.order_number}</b> — {service_name}",
        ]
        if package_name:
            lines.append(f"Paket: {package_name}")
        lines.append(f"Summa: <b>{order.total_price:,.0f}</b> so'm\n")
        lines.append(
            f"{STATUS_EMOJI.get(old_status, '')} {STATUS_LABEL.get(old_status, old_status)} → "
            f"{STATUS_EMOJI.get(new_status, '')} <b>{STATUS_LABEL.get(new_status, new_status)}</b>"
        )
        if new_status == 'cancelled' and order.cancel_reason:
            lines.append(f"\n❓ <b>Sabab:</b> {order.cancel_reason}")
        if new_status == 'completed':
            lines.append("\n🎉 Xizmat yetkazildi. O'yindan zavqlaning!")
        send_to_user(order.customer, '\n'.join(lines), reply_markup=_order_webapp_button(order))
    except Exception:
        logger.exception("Failed to notify order status")


def notify_payment_success(order, payment=None):
    """Notify the customer that their order payment succeeded."""
    try:
        if not order or not order.customer:
            return
        service_name = order.service.name if getattr(order, 'service', None) else 'Xizmat'
        text = (
            f"💰 <b>To'lov qabul qilindi!</b>\n\n"
            f"<b>#{order.order_number}</b> — {service_name}\n"
            f"Summa: <b>{order.total_price:,.0f}</b> so'm\n"
            f"Usul: Balans\n\n"
            f"🚀 Buyurtma operatorlar tomonidan tez orada bajariladi."
        )
        send_to_user(order.customer, text, reply_markup=_order_webapp_button(order))
    except Exception:
        logger.exception("Failed to notify payment success")


def notify_topup_status(user, amount, status: str, balance_after=None):
    """Notify a user that their balance top-up was approved or rejected."""
    try:
        if status == 'completed':
            text = (
                f"✅ <b>Balans to'ldirildi!</b>\n\n"
                f"Hisobingizga <b>{amount:,.0f}</b> so'm qo'shildi.\n"
                f"Joriy balans: <b>{balance_after:,.0f}</b> so'm\n\n"
                f"🎮 Endi istalgan o'yinga donat qilishingiz mumkin!"
            )
        elif status == 'cancelled':
            text = (
                f"❌ <b>Balans to'ldirish rad etildi</b>\n\n"
                f"So'rov: <b>{amount:,.0f}</b> so'm\n\n"
                f"Sababni bilish uchun operator bilan bog'laning."
            )
        else:
            return
        send_to_user(user, text)
    except Exception:
        logger.exception("Failed to notify top-up status")
