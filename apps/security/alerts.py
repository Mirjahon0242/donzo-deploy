"""
Telegram Alert System (DONZO Security).

HIGH/CRITICAL incidents trigger real-time alerts to:
  1. the report group (payment_report_chat_id) with inline action buttons
  2. the emergency Telegram account (CRITICAL only) with escalation
  3. repeated escalation when nobody acknowledges within the timeout

Inline buttons: Approve / Reject / Keep Hold / Block User / Open Case.
The bot (bot.py) handles the callbacks and routes them to
services.resolve_incident().

Acknowledgement: an incident stays OPEN until an admin ACKs or resolves it.
Escalation levels are tracked on the incident.
"""
import logging
from decimal import Decimal

from django.utils import timezone

from apps.settings_app.models import Setting
from .models import SecurityAlert, SecurityIncident
from .risk_engine import get_security_settings

logger = logging.getLogger(__name__)


def _fmt_user(user):
    if user is None:
        return '—'
    return f"@{user.telegram_username or user.username}"


def _incident_text(incident, s) -> str:
    user = incident.user
    lines = [
        "🚨 <b>DONZO SECURITY ALERT</b>\n",
        f"Risk: <b>{incident.severity}</b>",
        f"Score: {incident.risk_score}/100",
        f"User: {_fmt_user(user)}",
        f"User ID: <code>{user.telegram_id if user else '—'}</code>",
        f"Payment: <b>{incident.payment_amount:,.0f}</b> so'm",
        f"Request: <code>#{incident.topup_request_id or '—'}</code>",
        f"Incident: <code>#{incident.id}</code>",
    ]
    if incident.related_game_ids:
        lines.append(f"Game IDs (oxirgi 4): {', '.join(incident.related_game_ids[:6])}")
    if incident.reasons:
        lines.append("\n<b>Sabablar:</b>")
        lines += [f"• {r}" for r in incident.reasons[:8]]
    if incident.ai_summary:
        lines.append(f"\n<b>AI:</b> {incident.ai_summary[:200]}")
    lines.append("\n<b>Tavsiya:</b> HOLD + manual review")
    return "\n".join(lines)


def _action_buttons(incident_id: int):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    kb = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"sec:{incident_id}:approve"),
            InlineKeyboardButton("❌ Reject", callback_data=f"sec:{incident_id}:reject"),
        ],
        [
            InlineKeyboardButton("⏸ Keep Hold", callback_data=f"sec:{incident_id}:keep"),
            InlineKeyboardButton("🚫 Block User", callback_data=f"sec:{incident_id}:block"),
            InlineKeyboardButton("📁 Open Case", callback_data=f"sec:{incident_id}:case"),
        ],
    ]
    return InlineKeyboardMarkup(kb)


def _send(bot_token, chat_id, text, reply_markup=None) -> bool:
    import json
    import urllib.request
    try:
        url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
        payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML',
                   'disable_web_page_preview': True}
        if reply_markup is not None:
            payload['reply_markup'] = reply_markup.to_json() if hasattr(reply_markup, 'to_json') else reply_markup
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return bool(data.get('ok'))
    except Exception:
        logger.exception('alert send failed')
        return False


def _bot_token():
    return (Setting.get_setting('telegram_bot_token', '') or '').strip()


def notify_incident(incident: SecurityIncident) -> dict:
    """Send the initial alert for an incident. Returns {sent, escalation}."""
    s = get_security_settings()
    bot_token = _bot_token()
    if not bot_token:
        return {'sent': False, 'escalation': 0}

    text = _incident_text(incident, s)
    group_id = (Setting.get_setting('payment_report_chat_id', '') or '').strip()
    emergency = s.get('emergency_telegram_id', '')

    sent = 0
    recipients = []

    high_enabled = (Setting.get_setting('security_high_alerts_enabled', 'True') or '').lower() == 'true'
    crit_enabled = (Setting.get_setting('security_critical_alerts_enabled', 'True') or '').lower() == 'true'

    if incident.severity == 'CRITICAL' and not crit_enabled:
        pass
    elif incident.severity == 'HIGH' and not high_enabled:
        pass
    else:
        if group_id:
            if _send(bot_token, group_id, text, _action_buttons(incident.id)):
                sent += 1
                recipients.append(group_id)
        # CRITICAL also goes straight to the emergency account
        if incident.severity == 'CRITICAL' and emergency:
            if _send(bot_token, emergency, "🔴 <b>URGENT</b>\n\n" + text, _action_buttons(incident.id)):
                sent += 1
                recipients.append(emergency)

    for r in recipients:
        SecurityAlert.objects.create(
            incident=incident,
            alert_type=incident.severity,
            severity=incident.severity,
            recipient=r,
            message_text=text[:500],
            status='SENT',
            escalation_level=0,
        )

    incident.add_timeline('alert_sent', f"{sent} ta alert yuborildi")
    return {'sent': sent > 0, 'escalation': sent}


def escalate_open_incidents() -> int:
    """
    Escalation sweeper (called periodically by the user client).

    Finds OPEN incidents older than ack_timeout and re-alerts:
      level 1 → repeat to the group
      level 2 → emergency Telegram account
      level 3 → secondary admin account
    Returns the number of escalations sent.
    """
    s = get_security_settings()
    bot_token = _bot_token()
    if not bot_token:
        return 0

    ack_timeout = s.get('ack_timeout_min', 2)
    escal_timeout = s.get('escalation_timeout_min', 5)
    now = timezone.now()

    from datetime import timedelta
    from django.db.models import Q

    escalated = 0
    # CRITICAL: escalate past ack_timeout; HIGH: past escal_timeout
    criticals = SecurityIncident.objects.filter(
        severity='CRITICAL', status__in=['OPEN', 'INVESTIGATING'],
        created_at__lt=now - timedelta(minutes=ack_timeout),
        escalation_level__lt=3,
    )
    highs = SecurityIncident.objects.filter(
        severity='HIGH', status__in=['OPEN', 'INVESTIGATING'],
        created_at__lt=now - timedelta(minutes=escal_timeout),
        escalation_level__lt=2,
    )

    for incident in list(criticals) + list(highs):
        level = incident.escalation_level + 1
        recipients = []

        # level 1: report group; level 2: emergency; level 3: secondary admin
        if level >= 1:
            g = (Setting.get_setting('payment_report_chat_id', '') or '').strip()
            if g:
                recipients.append(('group', g))
        if level >= 2 and incident.severity == 'CRITICAL':
            if s.get('emergency_telegram_id'):
                recipients.append(('emergency', s['emergency_telegram_id']))
        if level >= 3:
            if s.get('secondary_admin_id'):
                recipients.append(('secondary', s['secondary_admin_id']))

        text = ("🔔 <b>ESCALATION</b> (daraja {}) — javob kutilmoqda\n\n".format(level)
                + _incident_text(incident, s))
        sent_any = False
        for kind, chat in recipients:
            if _send(bot_token, chat, text, _action_buttons(incident.id)):
                sent_any = True
                SecurityAlert.objects.create(
                    incident=incident, alert_type=incident.severity,
                    severity=incident.severity, recipient=chat,
                    message_text=text[:500], status='ESCALATED',
                    escalation_level=level,
                )
        if sent_any:
            incident.escalation_level = level
            incident.add_timeline('escalation', f"Daraja {level} — yuborildi")
            escalated += 1

    return escalated


def acknowledge_incident(incident_id: int, actor_name: str) -> None:
    """Mark an incident as acknowledged (stops escalation)."""
    from django.db import transaction
    with transaction.atomic():
        incident = SecurityIncident.objects.get(pk=incident_id)
        if incident.status == 'OPEN':
            incident.status = 'ACKED'
            incident.acked_at = timezone.now()
            incident.add_timeline('acknowledged', actor_name)
        SecurityAlert.objects.filter(incident=incident, status='ESCALATED').update(status='ACKED')
    _audit('alert_acknowledged', f"Incident #{incident_id} ACK: {actor_name}")


def _audit(action, description):
    try:
        from apps.audit_log.models import AuditLog
        AuditLog.objects.create(action=action, target_type='SecurityIncident', description=description)
    except Exception:
        logger.exception('audit failed')


def ack_summary_text() -> str:
    """Security status text for the Saved Messages 'security' command."""
    from django.db.models import Sum
    from apps.cardpay.models import CardTopupRequest
    now = timezone.now()
    today = now.date()

    paid_today = CardTopupRequest.objects.filter(status='paid', paid_at__date=today)
    total_today = paid_today.aggregate(t=Sum('unique_amount'))['t'] or 0
    open_high = SecurityIncident.objects.filter(severity='HIGH', status__in=['OPEN', 'ACKED', 'INVESTIGATING']).count()
    open_crit = SecurityIncident.objects.filter(severity='CRITICAL', status__in=['OPEN', 'ACKED', 'INVESTIGATING']).count()

    return (
        "🔐 <b>DONZO SECURITY STATUS</b>\n\n"
        f"System: <b>ONLINE</b>\n"
        f"Payment Listener: ONLINE\n"
        f"Gemini: {gemini_ai.health_check().get('reachable') and 'ONLINE' or 'OFFLINE'}\n\n"
        f"Today Volume: <b>{total_today:,.0f}</b> so'm\n"
        f"Approved: <b>{paid_today.count()}</b>\n"
        f"Hold: <b>{open_high}</b>\n"
        f"Suspicious: <b>{open_high + open_crit}</b>\n"
        f"HIGH: <b>{open_high}</b>\n"
        f"CRITICAL: <b>{open_crit}</b>\n\n"
        f"Lockdown: {('ON' if get_security_settings()['lockdown'] else 'OFF')}\n"
        f"Shadow mode: {('ON' if get_security_settings()['shadow_mode'] else 'OFF')}"
    )
