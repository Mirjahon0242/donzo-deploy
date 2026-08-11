"""
DONZO Security orchestrator.

Flow for every matched payment:
  1. Rule Engine (deterministic, always runs)
  2. Gemini AI (optional; fail-safe when down)
  3. Decision Engine (rules + AI + history + admin flags + lockdown)
  4. Persist PaymentRiskAssessment + RiskEvents
  5. HIGH/CRITICAL → SecurityIncident + alerts + (shadow mode: no impact)
  6. Update UserRiskProfile

The decision is RETURNED to the caller (cardpay.consume_payment_message),
which is the DECISION ENFORCER: it credits the balance ONLY when the
decision is APPROVED and the amount is below the suspicious limit.
"""
import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from . import gemini_ai, risk_engine
from .models import (
    APPROVED, BLOCKED, HIGH, HOLD, LOW, MANUAL_REVIEW, CRITICAL,
    MEDIUM, PaymentRiskAssessment, RiskEvent, SecurityIncident,
    UserRiskProfile, AI_UNAVAILABLE,
)

logger = logging.getLogger(__name__)

# Decisions that the enforcer must NOT credit
NON_CREDIT_DECISIONS = {HOLD, MANUAL_REVIEW, BLOCKED}


def _build_payload(user, amount, rule_result, request, now) -> dict:
    """Minimal non-sensitive context for Gemini."""
    vol = risk_engine._user_volume_windows(user.id, now, request.id if request else None)
    hist = risk_engine._user_history(user, now)
    return {
        'requested_amount': float(request.requested_amount) if request else float(amount),
        'received_amount': float(amount),
        'account_age_days': (now - user.date_joined).days if user.date_joined else 999,
        'payment_count': hist['paid_count'],
        'lifetime_volume': float(hist['lifetime_volume']),
        'volume_10m': float(vol['10m']),
        'volume_1h': float(vol['1h']),
        'volume_24h': float(vol['24h']),
        'volume_7d': float(vol['7d']),
        'failed_count': hist['failed_count'],
        'incidents_24h': hist['incidents_24h'],
        'recent_payment_amounts': _recent_amounts(user, now),
        'risk_score_rules': rule_result.score,
        'suspicious_limit': risk_engine.get_security_settings()['suspicious_limit'],
    }


def _recent_amounts(user, now, n=5):
    from apps.cardpay.models import CardTopupRequest
    return [float(r.unique_amount) for r in
            CardTopupRequest.objects.filter(user=user, status='paid')
            .order_by('-paid_at')[:n]]


def _decide(rule_result, ai, s, user, amount, request) -> dict:
    """Final decision: rules + AI + history + hard signals.

    Returns {decision, final_score, level, reasons, patterns, ai_error}
    """
    rule_score = rule_result.score
    reasons = list(rule_result.reasons)
    patterns = [e['rule'] for e in rule_result.events]

    # Hard signals that trump scoring
    flag = risk_engine.incident_flag_from_user(user)
    if flag == 'blocked':
        return {'decision': BLOCKED, 'final_score': 100, 'level': CRITICAL,
                'reasons': reasons + ['Admin tomonidan bloklangan'],
                'patterns': patterns, 'ai_error': ''}
    blacklisted = any('Blacklist' == e['rule'] for e in rule_result.events)
    if blacklisted:
        return {'decision': BLOCKED, 'final_score': 100, 'level': CRITICAL,
                'reasons': reasons, 'patterns': patterns, 'ai_error': ''}

    # AI merge (fail-safe)
    ai_score = None
    ai_error = ''
    if ai is not None and ai.get('ok'):
        ai_score = ai['risk_score']
        reasons += [f"Gemini: {r}" for r in ai.get('reasons', [])][:6]
        patterns += ai.get('detected_patterns', [])
    elif ai is not None and not ai.get('ok'):
        ai_error = ai.get('error', 'unavailable')

    final_score = max(rule_score, ai_score if ai_score is not None else 0)
    final_score = min(100, final_score)
    level = risk_engine.score_to_level(final_score, s)

    # Lockdown: hold large payments
    if s['lockdown'] and amount > Decimal(s['new_user_max']) and level in (LOW, MEDIUM):
        return {'decision': HOLD, 'final_score': final_score, 'level': level,
                'reasons': reasons + ['Lockdown rejimi: katta to\'lov ushlandi'],
                'patterns': patterns, 'ai_error': ai_error}

    if level == LOW:
        decision = APPROVED
    elif level == MEDIUM:
        decision = APPROVED  # extra monitoring, no disruption
    elif level == HIGH:
        decision = HOLD
    else:  # CRITICAL
        decision = BLOCKED if (ai is not None and ai.get('ok') and ai.get('recommended_action') == 'BLOCK') else HOLD

    return {'decision': decision, 'final_score': final_score, 'level': level,
            'reasons': reasons, 'patterns': patterns, 'ai_error': ai_error}


def evaluate_payment(user, amount, request=None, message=None, force=False) -> dict:
    """
    Full risk evaluation of one payment. Returns the decision dict.

    Called by cardpay.consume_payment_message (in the background user-client
    thread — never inside a web request).

    force: run even if shadow mode would skip persistence side effects.
    """
    s = risk_engine.get_security_settings()
    now = timezone.now()

    # Rule engine — deterministic, always runs (read-only, no locks held).
    rule_result = risk_engine.evaluate_rules(user, amount, request, now)

    # Gemini — external HTTP (up to 20s). Kept OUTSIDE the persistence
    # transaction so a slow AI call never holds a DB write lock.
    ai = None
    if s['ai_enabled']:
        payload = _build_payload(user, amount, rule_result, request, now)
        ai = gemini_ai.analyze(payload)

    # Merged (rules + AI) decision — what the full engine would decide.
    merged = _decide(rule_result, ai, s, user, amount, request)

    # Shadow mode: AI observes, never enforces. The enforced decision is the
    # rules-only one; the AI-merged decision is recorded as shadow_decision.
    # BLOCKED from an admin flag / blacklist is a HUMAN decision and is
    # never softened (already returned early by _decide).
    outcome_decision = merged['decision']
    shadow_decision = ''
    if s['shadow_mode'] and ai is not None and ai.get('ok'):
        # A HUMAN BLOCK (admin flag / blacklist) is NEVER softened by shadow
        # mode — shadow mode only removes AI influence, never a human decision.
        human_blocked = (risk_engine.incident_flag_from_user(user) == 'blocked'
                         or any(e['rule'] == 'Blacklist' for e in rule_result.events))
        if human_blocked:
            outcome_decision = BLOCKED
        else:
            rule_level = risk_engine.score_to_level(rule_result.score, s)
            if s['lockdown'] and amount > Decimal(s['new_user_max']):
                outcome_decision = HOLD  # lockdown always enforces
            elif rule_level in (LOW, MEDIUM):
                outcome_decision = APPROVED
            elif rule_level == HIGH:
                outcome_decision = HOLD
            else:
                outcome_decision = BLOCKED
        shadow_decision = merged['decision']

    with transaction.atomic():
        # Persist assessment + events
        assessment = PaymentRiskAssessment.objects.create(
            payment_message=message,
            topup_request=request,
            user=user,
            requested_amount=request.requested_amount if request else amount,
            received_amount=amount,
            rule_score=rule_result.score,
            ai_score=ai['risk_score'] if ai and ai.get('ok') else None,
            final_score=merged['final_score'],
            risk_level=merged['level'],
            ai_confidence=ai['confidence'] if ai and ai.get('ok') else None,
            ai_available=bool(ai and ai.get('ok')),
            ai_error=merged['ai_error'],
            shadow_mode=s['shadow_mode'],
            ai_summary=ai['admin_summary'] if ai and ai.get('ok') else '',
            reasons=merged['reasons'],
            detected_patterns=merged['patterns'],
            rule_triggers=[e['rule'] for e in rule_result.events],
            decision=outcome_decision,
            shadow_decision=shadow_decision,
        )
        for e in rule_result.events:
            RiskEvent.objects.create(
                assessment=assessment,
                rule_name=e['rule'],
                points=e['points'],
                severity=e['severity'],
                description=e['description'],
                meta=e.get('meta', {}),
            )

        # User profile refresh
        _update_profile(user)

        # Create incidents for HIGH/CRITICAL outcomes (also in shadow mode)
        incident = None
        if merged['level'] in (HIGH, CRITICAL):
            incident = _create_incident(assessment, user, request, merged, s)

    return {
        'decision': outcome_decision,
        'level': merged['level'],
        'final_score': merged['final_score'],
        'shadow_mode': s['shadow_mode'],
        'assessment_id': assessment.id,
        'incident_id': incident.id if incident else None,
        'reasons': merged['reasons'],
        'patterns': merged['patterns'],
        'ai_error': merged['ai_error'],
        'ai_available': bool(ai and ai.get('ok')),
    }


def _create_incident(assessment, user, request, decision, s):
    """Create a SecurityIncident for HIGH/CRITICAL events."""
    incident = SecurityIncident.objects.create(
        user=user,
        assessment=assessment,
        topup_request=request,
        severity=decision['level'],
        risk_score=decision['final_score'],
        status='OPEN',
        payment_amount=assessment.received_amount,
        rule_triggers=list(assessment.rule_triggers or []),
        ai_summary=assessment.ai_summary,
        reasons=assessment.reasons,
        related_game_ids=_user_game_ids(user),
    )
    incident.add_timeline('incident_created',
                          f"Risk {decision['final_score']} ({decision['level']}) — {decision['decision']}")
    return incident


def _user_game_ids(user):
    """Game account ids the user ordered before (masked: last 4 chars only)."""
    from apps.orders.models import Order
    ids = set()
    for field_name in ('player_id', 'user_id', 'account', 'game_id', 'login'):
        try:
            vals = Order.objects.filter(customer=user).values_list(field_name, flat=True)
            for v in vals:
                if v:
                    v = str(v)
                    ids.add(v if len(v) <= 4 else v[-4:])
        except Exception:
            continue
    return sorted(ids)[:20]


def _update_profile(user):
    """Refresh the user's lifetime risk snapshot."""
    from django.db.models import Sum
    from apps.cardpay.models import CardTopupRequest
    from .models import SecurityIncident

    now = timezone.now()
    paid = CardTopupRequest.objects.filter(user=user, status='paid')
    lifetime = paid.aggregate(t=Sum('unique_amount'))['t'] or 0
    v24 = paid.filter(paid_at__gte=now - timezone.timedelta(hours=24)).aggregate(t=Sum('unique_amount'))['t'] or 0
    v7d = paid.filter(paid_at__gte=now - timezone.timedelta(days=7)).aggregate(t=Sum('unique_amount'))['t'] or 0

    profile, _ = UserRiskProfile.objects.get_or_create(user=user)
    profile.lifetime_volume = lifetime
    profile.volume_24h = v24
    profile.volume_7d = v7d
    profile.payment_count = paid.count()
    profile.failed_count = CardTopupRequest.objects.filter(
        user=user, status__in=['cancelled', 'expired']).count()
    profile.suspicious_count = SecurityIncident.objects.filter(user=user).count()
    profile.hold_count = SecurityIncident.objects.filter(
        user=user, severity__in=[HIGH, CRITICAL], status__in=['OPEN', 'ACKED', 'INVESTIGATING']).count()
    profile.game_ids = _user_game_ids(user)
    profile.save()


def resolve_incident(incident_id, actor, action: str, note: str = ''):
    """
    Admin actions on an incident. Actions:
      approve — credit the held payment (decision enforcer), resolve incident
      reject  — cancel the payment, resolve incident
      block   — block the user + BLOCK decision, resolve incident
      keep    — keep HOLD (investigating)
    """
    from apps.cardpay import services as cardpay_services
    from .models import REJECTED

    with transaction.atomic():
        incident = SecurityIncident.objects.select_for_update().get(pk=incident_id)
        if incident.status in ('RESOLVED', 'FALSE_POSITIVE', 'CONFIRMED_FRAUD'):
            return {'ok': False, 'detail': f"Incident allaqachon: {incident.status}"}

        req = incident.topup_request
        credited = False
        credit_amount = None

        if action == 'approve':
            if req is not None and incident.assessment:
                # The request may have EXPIRED while the admin decided — the
                # money actually arrived, so allow_expired reopens the tx and
                # credits (same rule as approve_suspicious). Never credits a
                # request that is already paid/completed.
                res = cardpay_services.credit_request(req, allow_expired=True)
                credited = res.get('credited', False)
                credit_amount = str(res.get('credit_amount', '')) if credited else None
            incident.status = 'RESOLVED'
            incident.resolution_note = note or (
                f"Admin tasdiqladi: +{incident.payment_amount} so'm kredit qilindi"
                if credited else
                "Admin tasdiqladi — kredit amalga oshmadi (holatni tekshiring)"
            )
        elif action == 'reject':
            if req is not None and req.status == 'pending':
                req.status = 'cancelled'
                req.save(update_fields=['status', 'updated_at'])
                tx = req.balance_tx
                if tx is not None and tx.status == 'pending':
                    tx.status = 'cancelled'
                    tx.description = 'Xavfsizlik: admin rad etdi'
                    tx.save(update_fields=['status', 'description'])
            incident.status = 'RESOLVED'
            incident.resolution_note = note or 'Admin rad etdi — balansga tushmadi'
        elif action == 'block':
            user = incident.user
            if user is not None:
                profile, _ = UserRiskProfile.objects.get_or_create(user=user)
                profile.admin_flag = UserRiskProfile.BLOCKED
                profile.save()
                user.is_blacklisted = True
                user.save(update_fields=['is_blacklisted'])
            if req is not None and req.status == 'pending':
                req.status = 'cancelled'
                req.save(update_fields=['status', 'updated_at'])
                tx = req.balance_tx
                if tx is not None and tx.status == 'pending':
                    tx.status = 'cancelled'
                    tx.save(update_fields=['status'])
            incident.status = 'CONFIRMED_FRAUD'
            incident.resolution_note = note or 'Foydalanuvchi bloklandi'
        elif action == 'keep':
            incident.status = 'INVESTIGATING'
            incident.resolution_note = note or ''
        else:
            return {'ok': False, 'detail': 'Noma\'lum harakat'}

        incident.resolved_by = actor
        incident.resolved_at = timezone.now()
        incident.add_timeline(f'admin:{action}', note or actor.username)

    _audit_security(f'incident_{action}', actor, f"Incident #{incident.id}: {action} ({note or ''})")
    return {
        'ok': True,
        'credited': credited,
        'credit_amount': credit_amount,
        'incident_status': incident.status,
    }


def _audit_security(action, user, description):
    try:
        from apps.audit_log.models import AuditLog
        AuditLog.objects.create(user=user, action=action, target_type='SecurityIncident',
                                description=description)
    except Exception:
        logger.exception('audit failed')
