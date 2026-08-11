"""
Deterministic Rule Engine (DONZO Security).

Layer A of the risk engine. Pure rules — no AI. Runs even when Gemini is
down. Every rule contributes explainable points to a 0–100 risk score.

Rule philosophy: a SINGLE signal is never enough to convict. We collect
evidence and let the Decision Engine weigh the whole picture.
"""
import json
import logging
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from apps.settings_app.models import Setting
from .models import LOW, MEDIUM, HIGH, CRITICAL

logger = logging.getLogger(__name__)

# ── Setting keys ──
K_AI_ENABLED = 'security_ai_enabled'
K_SHADOW = 'security_shadow_mode'
K_FAIL_OPEN = 'security_fail_open'
K_LOW_MAX = 'risk_low_max'
K_MED_MAX = 'risk_medium_max'
K_HIGH_MAX = 'risk_high_max'
K_V10 = 'velocity_10m_limit'
K_V1H = 'velocity_1h_limit'
K_V24H = 'velocity_24h_limit'
K_V7D = 'velocity_7d_limit'
K_NEW_USER_MAX = 'new_user_max_payment'
K_LOCKDOWN = 'security_lockdown'
K_BLACKLIST = 'security_blacklist'
K_WHITELIST = 'security_whitelist'


def _int(key, default):
    try:
        return int(Setting.get_setting(key, default) or default)
    except (TypeError, ValueError):
        return default


def get_security_settings() -> dict:
    """Central settings snapshot used by the whole security subsystem."""
    return {
        'ai_enabled': (Setting.get_setting(K_AI_ENABLED, 'False') or '').lower() == 'true',
        'shadow_mode': (Setting.get_setting(K_SHADOW, 'True') or '').lower() == 'true',
        'fail_open': (Setting.get_setting(K_FAIL_OPEN, 'False') or '').lower() == 'true',
        'gemini_api_key': (Setting.get_setting('gemini_api_key', '') or '').strip(),
        'gemini_model': Setting.get_setting('gemini_model', 'gemini-3.1-flash-lite'),
        'low_max': _int(K_LOW_MAX, 29),
        'med_max': _int(K_MED_MAX, 49),
        'high_max': _int(K_HIGH_MAX, 69),
        'v10m': _int(K_V10, 200_000),
        'v1h': _int(K_V1H, 500_000),
        'v24h': _int(K_V24H, 1_500_000),
        'v7d': _int(K_V7D, 5_000_000),
        'new_user_max': _int(K_NEW_USER_MAX, 300_000),
        'lockdown': (Setting.get_setting(K_LOCKDOWN, 'False') or '').lower() == 'true',
        'blacklist': _parse_list(K_BLACKLIST),
        'whitelist': _parse_list(K_WHITELIST),
        'suspicious_limit': _int('payment_suspicious_limit', 500_000),
        'emergency_telegram_id': (Setting.get_setting('emergency_telegram_id', '') or '').strip(),
        'secondary_admin_id': (Setting.get_setting('security_secondary_admin_id', '') or '').strip(),
        'ack_timeout_min': _int('security_ack_timeout_min', 2),
        'escalation_timeout_min': _int('security_escalation_timeout_min', 5),
    }


def _parse_list(key):
    raw = Setting.get_setting(key, '') or ''
    items = [x.strip().lstrip('@').lower() for x in str(raw).split(',') if x.strip()]
    return items


def score_to_level(score: int, s: dict) -> str:
    if score <= s['low_max']:
        return LOW
    if score <= s['med_max']:
        return MEDIUM
    if score <= s['high_max']:
        return HIGH
    return CRITICAL


def _user_volume_windows(user_id, now, exclude_request_id=None):
    """Sum of completed top-ups in each window (10m/1h/24h/7d).

    Counts BalanceTransaction (tx_type=topup, status=completed) ONLY. Every
    credit path — card auto-match (credit_request), admin approve, suspicious
    approve — completes its BalanceTransaction, so also summing
    CardTopupRequest rows here would DOUBLE-count every card payment and
    inflate the velocity rules.
    """
    from apps.payments.models import BalanceTransaction

    exclude_tx_ids = []
    if exclude_request_id:
        from apps.cardpay.models import CardTopupRequest
        try:
            req = CardTopupRequest.objects.only('balance_tx_id').get(id=exclude_request_id)
            if req.balance_tx_id:
                exclude_tx_ids = [req.balance_tx_id]
        except CardTopupRequest.DoesNotExist:
            pass

    out = {'10m': 0, '1h': 0, '24h': 0, '7d': 0}
    for label, hours in (('10m', 10 / 60), ('1h', 1), ('24h', 24), ('7d', 24 * 7)):
        since = now - timedelta(hours=hours)
        qs = BalanceTransaction.objects.filter(
            user_id=user_id, tx_type='topup', status='completed',
            created_at__gte=since,
        )
        if exclude_tx_ids:
            qs = qs.exclude(id__in=exclude_tx_ids)
        out[label] = qs.aggregate(t=Sum('amount'))['t'] or Decimal(0)
    return out


def _user_history(user, now):
    """Lifetime stats for a user (payments, failures, incidents)."""
    from apps.cardpay.models import CardTopupRequest
    from .models import SecurityIncident

    paid = CardTopupRequest.objects.filter(user=user, status='paid')
    lifetime = paid.aggregate(t=Sum('unique_amount'))['t'] or 0
    failed = CardTopupRequest.objects.filter(
        user=user, status__in=['cancelled', 'expired'],
    ).count()
    incidents_24h = SecurityIncident.objects.filter(
        user=user, created_at__gte=now - timedelta(hours=24),
    ).count()
    account_age_days = (now - user.date_joined).days if user.date_joined else 999
    return {
        'paid_count': paid.count(),
        'lifetime_volume': lifetime,
        'failed_count': failed,
        'incidents_24h': incidents_24h,
        'account_age_days': account_age_days,
    }


class RiskResult:
    """Collected evidence from the rule engine."""

    def __init__(self):
        self.events = []        # [{rule, points, severity, description, meta}]
        self.score = 0

    def add(self, rule, points, description, severity=MEDIUM, meta=None):
        if points == 0:
            return
        self.events.append({
            'rule': rule,
            'points': int(points),
            'severity': severity,
            'description': description,
            'meta': meta or {},
        })
        self.score += int(points)

    @property
    def reasons(self):
        """Explainability lines: '+20 New account'."""
        return [f"{'+' if e['points'] >= 0 else ''}{e['points']} {e['rule']}" for e in self.events]


def evaluate_rules(user, amount: Decimal, request=None, now=None) -> RiskResult:
    """
    Run ALL deterministic rules against a payment.

    user  — the User doing the top-up
    amount — the received unique amount (the money that actually arrived)
    request — the matched CardTopupRequest (optional)
    """
    s = get_security_settings()
    now = now or timezone.now()
    res = RiskResult()

    # ── Hard signals first (can force BLOCK downstream) ──
    tg_id = str(user.telegram_id or '').lower()
    username = (user.telegram_username or user.username or '').lower()

    if tg_id in s['blacklist'] or username in s['blacklist']:
        res.add('Blacklist', 100, 'Foydalanuvchi admin tomonidan blacklistga kiritilgan', CRITICAL,
                {'source': 'blacklist'})
    if tg_id in s['whitelist'] or username in s['whitelist']:
        # Whitelisted → treated as fully trusted (score capped low)
        res.add('Whitelist', -80, 'Foydalanuvchi admin tomonidan whitelistda', LOW, {'source': 'whitelist'})

    # ── Account age ──
    age_days = (now - user.date_joined).days if user.date_joined else 999
    if age_days <= 1:
        res.add('New account', 20, f'Akkaunt {age_days} kunlik — juda yangi', HIGH, {'age_days': age_days})
    elif age_days <= 7:
        res.add('New account', 10, f'Akkaunt {age_days} kunlik', MEDIUM, {'age_days': age_days})

    # ── First payment large ──
    hist = _user_history(user, now)
    if hist['paid_count'] == 0 and amount > s['new_user_max']:
        res.add('High first payment', 20,
                f'Birinchi to\'lov {amount:,.0f} so\'m — yangi akkaunt uchun juda katta', HIGH)

    # ── Velocity (cumulative windows) ──
    vol = _user_volume_windows(user.id, now, request.id if request else None)
    # exclude this payment itself from the windows
    for label, limit, pts, sev in (
        ('10m', s['v10m'], 15, HIGH),
        ('1h', s['v1h'], 10, HIGH),
        ('24h', s['v24h'], 10, MEDIUM),
        ('7d', s['v7d'], 5, MEDIUM),
    ):
        v = vol[label]
        if v > limit:
            over = float(v) / float(limit)
            res.add(f'{label} velocity', pts,
                    f'So\'nggi {label} ichida {v:,.0f} so\'m to\'plangan (limit {limit:,.0f})',
                    sev, {'window': label, 'volume': str(v), 'limit': limit})

    # ── Split payments (below-limit chunks that add up over the limit) ──
    if vol['24h'] > s['suspicious_limit']:
        from apps.cardpay.models import CardTopupRequest
        recent = CardTopupRequest.objects.filter(
            user=user, status='paid', paid_at__gte=now - timedelta(hours=24),
        ).order_by('paid_at')
        chunks = [r for r in recent if r.unique_amount <= s['suspicious_limit']]
        if len(chunks) >= 3:
            res.add('Split payments', 15,
                    f'{len(chunks)} ta to\'lov (har biri limitdan past) — umumiy {vol["24h"]:,.0f} so\'m',
                    HIGH, {'chunks': len(chunks), 'total': str(vol['24h'])})

    # ── Repeated failures / consecutive suspicious ──
    if hist['failed_count'] >= 3:
        res.add('Many failed payments', 10,
                f'{hist["failed_count"]} ta bekor qilingan/muddati o\'tgan so\'rov', MEDIUM)
    if hist['incidents_24h'] >= 2:
        res.add('Consecutive incidents', 15,
                f'Oxirgi 24 soatda {hist["incidents_24h"]} ta xavfsizlik hodisasi', HIGH)

    # ── Late payment (expired window, money arrived anyway) ──
    if request is not None and request.status == 'expired':
        res.add('Late payment', 15, 'Muddati o\'tgan so\'rovga kechikib kelgan to\'lov', HIGH)

    # ── Lockdown mode ──
    if s['lockdown'] and amount > s['new_user_max']:
        res.add('Lockdown', 20, 'SECURITY LOCKDOWN rejimi — katta to\'lovlar ushlab turiladi', HIGH,
                {'mode': 'lockdown'})

    res.score = min(100, max(0, res.score))
    return res


def incident_flag_from_user(user) -> str:
    """Admin flag (blocked/watch) as an extra hard signal, if any."""
    try:
        profile = user.risk_profile
        return profile.admin_flag
    except Exception:
        return 'normal'
