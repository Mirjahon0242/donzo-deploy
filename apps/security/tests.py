"""
Tests for the DONZO Security / Anti-Fraud system.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.cardpay.models import CardTopupRequest
from apps.payments.models import BalanceTransaction
from apps.settings_app.models import Setting
from . import gemini_ai, risk_engine, services
from .models import (
    APPROVED, BLOCKED, HOLD, HIGH, LOW, MEDIUM, CRITICAL,
    PaymentRiskAssessment, RiskEvent, SecurityIncident, UserRiskProfile,
)

User = get_user_model()


def make_user(**kw):
    defaults = dict(username='u', email='u@tg.user', telegram_id='1000001')
    defaults.update(kw)
    return User.objects.create_user(**defaults)


def make_paid_request(user, amount):
    """A fully-paid card top-up: request paid AND its BalanceTransaction
    completed (exactly what credit_request does in production — the risk
    engine's velocity windows count completed transactions only)."""
    tx = BalanceTransaction.objects.create(
        user=user, tx_type='topup', amount=amount,
        balance_before=0, balance_after=amount, status='completed')
    req = CardTopupRequest.objects.create(
        user=user, balance_tx=tx, requested_amount=amount,
        unique_amount=amount, expires_at=timezone.now() + timedelta(minutes=10),
        status='paid', paid_at=timezone.now(),
    )
    return req


class RuleEngineTests(TestCase):
    def setUp(self):
        Setting.set_setting('security_ai_enabled', 'False')
        Setting.clear_cache()

    def test_new_account_large_first_payment(self):
        user = make_user(date_joined=timezone.now() - timedelta(hours=2))
        res = risk_engine.evaluate_rules(user, Decimal('400000'))
        self.assertGreaterEqual(res.score, 30)  # new account (20) + high first (20)
        names = [e['rule'] for e in res.events]
        self.assertIn('New account', names)
        self.assertIn('High first payment', names)

    def test_established_user_low_risk(self):
        user = make_user(date_joined=timezone.now() - timedelta(days=90))
        make_paid_request(user, 20000)
        res = risk_engine.evaluate_rules(user, Decimal('25000'))
        self.assertLessEqual(res.score, 10)

    def test_velocity_10m(self):
        user = make_user(date_joined=timezone.now() - timedelta(days=90))
        for i in range(6):
            make_paid_request(user, 50000)  # 300k in 10 minutes
        res = risk_engine.evaluate_rules(user, Decimal('50000'))
        self.assertTrue(any('10m' in e['rule'] for e in res.events))

    def test_split_payments_below_limit(self):
        """490k x4 each below limit → cumulative split risk."""
        Setting.set_setting('payment_suspicious_limit', '500000')
        Setting.clear_cache()
        user = make_user(date_joined=timezone.now() - timedelta(days=90))
        for i in range(4):
            make_paid_request(user, 490000)
        res = risk_engine.evaluate_rules(user, Decimal('490000'))
        self.assertTrue(any(e['rule'] == 'Split payments' for e in res.events))

    def test_blacklist_immediate(self):
        Setting.set_setting('security_blacklist', 'baduser')
        Setting.clear_cache()
        user = make_user(username='normal', telegram_username='baduser')
        res = risk_engine.evaluate_rules(user, Decimal('10000'))
        self.assertGreaterEqual(res.score, 100)
        self.assertTrue(any(e['rule'] == 'Blacklist' for e in res.events))

    def test_late_expired_payment(self):
        user = make_user(date_joined=timezone.now() - timedelta(days=90))
        req = CardTopupRequest.objects.create(
            user=user, requested_amount=10000, unique_amount=10000,
            expires_at=timezone.now() - timedelta(minutes=5), status='expired')
        res = risk_engine.evaluate_rules(user, Decimal('10000'), request=req)
        self.assertTrue(any(e['rule'] == 'Late payment' for e in res.events))


class DecisionTests(TestCase):
    def setUp(self):
        Setting.set_setting('security_ai_enabled', 'False')
        Setting.set_setting('security_shadow_mode', 'False')
        Setting.set_setting('risk_medium_max', '30')
        Setting.clear_cache()
        self.user = make_user(date_joined=timezone.now() - timedelta(days=90))

    def test_low_payment_approved(self):
        make_paid_request(self.user, 20000)
        out = services.evaluate_payment(self.user, Decimal('25000'))
        self.assertEqual(out['decision'], APPROVED)
        self.assertTrue(PaymentRiskAssessment.objects.exists())

    def test_high_risk_held(self):
        # Fresh account + big amount → rules 40 → HIGH (threshold 30) → HOLD
        user = make_user(username='fresh', email='fresh@tg.user',
                         telegram_id='1000002', date_joined=timezone.now() - timedelta(hours=1))
        out = services.evaluate_payment(user, Decimal('400000'))
        self.assertEqual(out['level'], HIGH)
        self.assertEqual(out['decision'], HOLD)
        self.assertIsNotNone(out['incident_id'])
        inc = SecurityIncident.objects.get(pk=out['incident_id'])
        self.assertEqual(inc.status, 'OPEN')

    def test_blocked_user(self):
        profile, _ = UserRiskProfile.objects.get_or_create(user=self.user)
        profile.admin_flag = UserRiskProfile.BLOCKED
        profile.save()
        out = services.evaluate_payment(self.user, Decimal('10000'))
        self.assertEqual(out['decision'], BLOCKED)

    def test_blacklisted_decision(self):
        Setting.set_setting('security_blacklist', self.user.username)
        Setting.clear_cache()
        out = services.evaluate_payment(self.user, Decimal('10000'))
        self.assertEqual(out['decision'], BLOCKED)

    def test_lockdown_holds_large(self):
        Setting.set_setting('security_lockdown', 'True')
        Setting.clear_cache()
        out = services.evaluate_payment(self.user, Decimal('400000'))
        self.assertEqual(out['decision'], HOLD)

    def test_shadow_mode_rules_enforce_ai_observes(self):
        """Shadow mode neutralizes AI influence ONLY — deterministic rules
        still enforce (new account + big first payment → rules HIGH → HOLD)."""
        Setting.set_setting('security_shadow_mode', 'True')
        Setting.set_setting('risk_medium_max', '30')
        Setting.clear_cache()
        user = make_user(username='shadow', email='shadow@tg.user',
                         telegram_id='1000003', date_joined=timezone.now() - timedelta(hours=1))
        out = services.evaluate_payment(user, Decimal('400000'))
        self.assertTrue(out['shadow_mode'])
        self.assertEqual(out['decision'], HOLD)  # rules enforce
        self.assertIsNotNone(out['incident_id'])  # admins still watch

    def test_shadow_mode_ai_opinion_not_enforced(self):
        """AI says CRITICAL/BLOCK but rules are LOW → shadow mode enforces
        the rules-only decision (APPROVED) and records what AI wanted."""
        Setting.set_setting('security_shadow_mode', 'True')
        Setting.set_setting('security_ai_enabled', 'True')
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('risk_medium_max', '30')
        Setting.clear_cache()
        user = make_user(username='shadai', email='shadai@tg.user',
                         telegram_id='10000011', date_joined=timezone.now() - timedelta(days=90))
        make_paid_request(user, 20000)
        ai_resp = {'ok': True, 'risk_score': 80, 'risk_level': 'CRITICAL',
                   'confidence': 0.9, 'reasons': ['AI: suspicious'],
                   'detected_patterns': ['ai_pattern'],
                   'recommended_action': 'BLOCK', 'admin_summary': 'AI thinks bad',
                   'requires_human_review': True}
        with mock.patch('apps.security.gemini_ai.analyze', return_value=ai_resp):
            out = services.evaluate_payment(user, Decimal('25000'))
        self.assertEqual(out['decision'], APPROVED)  # rules LOW → approved
        self.assertEqual(out['level'], CRITICAL)     # recorded merged level
        ass = PaymentRiskAssessment.objects.get(pk=out['assessment_id'])
        self.assertEqual(ass.decision, APPROVED)
        self.assertEqual(ass.shadow_decision, BLOCKED)  # what AI wanted

    def test_blocked_never_softened_by_shadow(self):
        """BLOCKED is a human decision — shadow mode must not approve it."""
        Setting.set_setting('security_shadow_mode', 'True')
        Setting.clear_cache()
        profile, _ = UserRiskProfile.objects.get_or_create(user=self.user)
        profile.admin_flag = UserRiskProfile.BLOCKED
        profile.save()
        out = services.evaluate_payment(self.user, Decimal('10000'))
        self.assertEqual(out['decision'], BLOCKED)

    def test_blocked_never_softened_by_shadow_even_with_ai(self):
        """Regression: with shadow mode ON and Gemini AVAILABLE, an admin-flag
        BLOCKED user (low rule score) must stay BLOCKED — the rules-only
        recompute must never turn a human block into APPROVED."""
        Setting.set_setting('security_shadow_mode', 'True')
        Setting.set_setting('security_ai_enabled', 'True')
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.clear_cache()
        profile, _ = UserRiskProfile.objects.get_or_create(user=self.user)
        profile.admin_flag = UserRiskProfile.BLOCKED
        profile.save()
        ai_resp = {'ok': True, 'risk_score': 5, 'risk_level': 'LOW',
                   'confidence': 0.9, 'reasons': ['AI: ok'],
                   'detected_patterns': [], 'recommended_action': 'ALLOW',
                   'admin_summary': 'AI sees nothing wrong',
                   'requires_human_review': False}
        with mock.patch('apps.security.gemini_ai.analyze', return_value=ai_resp):
            out = services.evaluate_payment(self.user, Decimal('10000'))
        self.assertEqual(out['decision'], BLOCKED)  # human block wins


class GeminiFailSafeTests(TestCase):
    def test_malformed_json_never_valid(self):
        """Schema validation must reject every malformed shape."""
        bad_inputs = [
            None, 'not json at all', [],
            {'risk_score': 'abc'},
            {'risk_score': 999, 'risk_level': 'LOW'},          # out of range
            {'risk_score': 50, 'risk_level': 'BANANA'},        # bad level
            {'risk_score': 50, 'risk_level': 'HIGH'},          # missing keys
            {'risk_score': -5, 'risk_level': 'LOW', 'confidence': 1.0,
             'reasons': [], 'detected_patterns': [], 'recommended_action': 'ALLOW',
             'admin_summary': 'x', 'requires_human_review': False},
            {'risk_score': 50, 'risk_level': 'LOW', 'confidence': 5.0,
             'reasons': [], 'detected_patterns': [], 'recommended_action': 'HACK',
             'admin_summary': 'x', 'requires_human_review': False},
        ]
        for bad in bad_inputs:
            self.assertIsNone(gemini_ai._validate_response(bad), f'should reject: {bad!r}')

    def test_validate_response_valid(self):
        data = {
            'risk_score': 60, 'risk_level': 'HIGH', 'confidence': 0.9,
            'reasons': ['x'], 'detected_patterns': ['y'],
            'recommended_action': 'HOLD', 'admin_summary': 's',
            'requires_human_review': True,
        }
        res = gemini_ai._validate_response(data)
        self.assertIsNotNone(res)
        self.assertEqual(res['risk_score'], 60)

    def test_ai_unavailable_fails_safe(self):
        """AI down + risky payment → HOLD (never auto-approve)."""
        Setting.set_setting('security_ai_enabled', 'True')
        Setting.set_setting('security_shadow_mode', 'False')
        Setting.set_setting('risk_medium_max', '30')
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.clear_cache()
        user = make_user(username='riskuser', email='risk@tg.user',
                         telegram_id='1000004', date_joined=timezone.now() - timedelta(hours=1))
        with mock.patch('apps.security.gemini_ai.analyze',
                        return_value={'ok': False, 'error': 'network_error'}):
            out = services.evaluate_payment(user, Decimal('400000'))
            self.assertEqual(out['decision'], HOLD)  # risky → HOLD, not APPROVE
            self.assertFalse(out['ai_available'])

    def test_ai_unavailable_safe_payment_still_ok(self):
        Setting.set_setting('security_ai_enabled', 'True')
        Setting.set_setting('security_shadow_mode', 'False')
        Setting.clear_cache()
        user = make_user(username='okuser', email='ok@tg.user',
                         telegram_id='1000005', date_joined=timezone.now() - timedelta(days=90))
        make_paid_request(user, 20000)
        with mock.patch('apps.security.gemini_ai.analyze',
                        return_value={'ok': False, 'error': 'timeout'}):
            out = services.evaluate_payment(user, Decimal('25000'))
            self.assertEqual(out['decision'], APPROVED)

    def test_sanitize_payload_strips_identifiers(self):
        payload = {'received_amount': 5000, 'username': 'hacker', 'chat_id': '-100x',
                   'game_id': '12345', 'volume_24h': 999999}
        safe = gemini_ai._sanitize_payload(payload)
        self.assertNotIn('username', safe)
        self.assertNotIn('chat_id', safe)
        self.assertNotIn('game_id', safe)
        self.assertIn('received_amount', safe)
        self.assertIn('volume_24h', safe)


class SecurityAPITests(TestCase):
    def setUp(self):
        Setting.set_setting('security_ai_enabled', 'False')
        Setting.set_setting('security_shadow_mode', 'False')
        Setting.set_setting('risk_medium_max', '30')
        Setting.clear_cache()
        self.admin = make_user(username='boss', email='boss@tg.user',
                               telegram_id='2007554600', role='super_admin')
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def test_dashboard_requires_auth(self):
        anon = APIClient()
        self.assertEqual(anon.get('/api/v1/admin/security/dashboard/').status_code, 401)

    def test_dashboard(self):
        r = self.client.get('/api/v1/admin/security/dashboard/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('stats', r.data)
        self.assertIn('ai', r.data)

    def test_settings_get_put(self):
        r = self.client.get('/api/v1/admin/security/settings/')
        self.assertEqual(r.status_code, 200)
        r = self.client.put('/api/v1/admin/security/settings/',
                            {'security_lockdown': 'True', 'velocity_24h_limit': '1000000'},
                            format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['lockdown'], True)

    def test_incident_approve_credits(self):
        user = make_user(username='victim', email='v@tg.user', telegram_id='1000006',
                         date_joined=timezone.now() - timedelta(hours=1))
        tx = BalanceTransaction.objects.create(
            user=user, tx_type='topup', amount=400000,
            balance_before=0, balance_after=0, status='pending')
        req = CardTopupRequest.objects.create(
            user=user, balance_tx=tx, requested_amount=400000,
            unique_amount=400000, expires_at=timezone.now() + timedelta(minutes=5),
            status='pending')
        out = services.evaluate_payment(user, Decimal('400000'), request=req)
        self.assertIn(out['decision'], (HOLD, BLOCKED))
        inc_id = out['incident_id']
        user.refresh_from_db()
        self.assertEqual(user.balance, 0)  # not credited

        r = self.client.post(f'/api/v1/admin/security/incidents/{inc_id}/approve/')
        self.assertEqual(r.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.balance, 400000)  # credited on admin approve

    def test_incident_reject_no_credit(self):
        user = make_user(username='victim2', email='v2@tg.user', telegram_id='1000007',
                         date_joined=timezone.now() - timedelta(hours=1))
        tx = BalanceTransaction.objects.create(
            user=user, tx_type='topup', amount=400000,
            balance_before=0, balance_after=0, status='pending')
        req = CardTopupRequest.objects.create(
            user=user, balance_tx=tx, requested_amount=400000,
            unique_amount=400000, expires_at=timezone.now() + timedelta(minutes=5),
            status='pending')
        out = services.evaluate_payment(user, Decimal('400000'), request=req)
        inc_id = out['incident_id']
        r = self.client.post(f'/api/v1/admin/security/incidents/{inc_id}/reject/')
        self.assertEqual(r.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.balance, 0)

    def test_block_user(self):
        user = make_user(username='bad', email='bad@tg.user', telegram_id='1000008')
        r = self.client.post(f'/api/v1/admin/security/profiles/{user.id}/block/')
        self.assertEqual(r.status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.is_blacklisted)

    def test_customer_cannot_access(self):
        customer = make_user(username='cust', email='c@tg.user', telegram_id='1000009',
                             role='customer')
        c = APIClient()
        c.force_authenticate(customer)
        self.assertEqual(c.get('/api/v1/admin/security/dashboard/').status_code, 403)


class SecurityRegressionTests(TestCase):
    """Regression tests for security bugs found in the deep audit."""

    def setUp(self):
        Setting.set_setting('security_ai_enabled', 'False')
        Setting.set_setting('security_shadow_mode', 'False')
        Setting.set_setting('risk_medium_max', '30')
        Setting.clear_cache()
        self.admin = make_user(username='boss3', email='boss3@tg.user',
                               telegram_id='2007554603', role='super_admin')
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def _held_incident(self):
        user = make_user(username='persist', email='persist@tg.user', telegram_id='10000012',
                         date_joined=timezone.now() - timedelta(hours=1))
        tx = BalanceTransaction.objects.create(
            user=user, tx_type='topup', amount=400000,
            balance_before=0, balance_after=0, status='pending')
        req = CardTopupRequest.objects.create(
            user=user, balance_tx=tx, requested_amount=400000,
            unique_amount=400000, expires_at=timezone.now() + timedelta(minutes=5),
            status='pending')
        out = services.evaluate_payment(user, Decimal('400000'), request=req)
        return out['incident_id']

    def test_incident_resolution_persists_to_db(self):
        """Regression: resolve_incident must persist status/resolved_at — the
        old add_timeline(update_fields=[...]) silently dropped them, leaving
        the incident OPEN in the DB and re-escalating forever."""
        inc_id = self._held_incident()
        r = self.client.post(f'/api/v1/admin/security/incidents/{inc_id}/reject/')
        self.assertEqual(r.status_code, 200)
        inc = SecurityIncident.objects.get(pk=inc_id)
        self.assertEqual(inc.status, 'RESOLVED')
        self.assertIsNotNone(inc.resolved_at)
        self.assertIsNotNone(inc.resolved_by)
        # The idempotency guard now reads the PERSISTED status → refuse repeat
        r2 = self.client.post(f'/api/v1/admin/security/incidents/{inc_id}/approve/')
        self.assertEqual(r2.status_code, 400)

    def test_escalation_level_persists_and_stops(self):
        """Regression: escalation_level must persist so the sweeper stops
        after the configured max (HIGH → level 2) instead of alerting forever."""
        from apps.security import alerts
        Setting.set_setting('security_escalation_timeout_min', '0')
        Setting.set_setting('security_ack_timeout_min', '0')
        Setting.set_setting('telegram_bot_token', '123456:TEST')
        Setting.set_setting('payment_report_chat_id', '-100123')
        Setting.clear_cache()
        inc = SecurityIncident.objects.create(
            severity='HIGH', risk_score=60, status='OPEN',
            created_at=timezone.now() - timedelta(minutes=10),
            payment_amount=100000)
        with mock.patch.object(alerts, '_send', return_value=True):
            n1 = alerts.escalate_open_incidents()
        inc.refresh_from_db()
        self.assertEqual(n1, 1)
        self.assertEqual(inc.escalation_level, 1)
        with mock.patch.object(alerts, '_send', return_value=True):
            n2 = alerts.escalate_open_incidents()
        inc.refresh_from_db()
        self.assertEqual(n2, 1)
        self.assertEqual(inc.escalation_level, 2)  # HIGH max level
        with mock.patch.object(alerts, '_send', return_value=True) as send:
            n3 = alerts.escalate_open_incidents()
        self.assertEqual(n3, 0)  # stopped — no infinite alert loop
        send.assert_not_called()

    def test_security_error_fails_closed_when_not_configured(self):
        """Regression: security_fail_open=False → a security-engine crash must
        HOLD the payment, never silently credit it."""
        from apps.cardpay import services as cs
        Setting.set_setting('payment_card_monitor_enabled', 'True')
        Setting.set_setting('security_fail_open', 'False')
        Setting.clear_cache()
        user = make_user()
        tx = BalanceTransaction.objects.create(
            user=user, tx_type='topup', amount=10000,
            balance_before=0, balance_after=0, status='pending')
        req = cs.create_topup_request(user, tx, Decimal('10000'))
        with mock.patch('apps.security.services.evaluate_payment',
                        side_effect=RuntimeError('boom')):
            r = cs.consume_payment_message('c', 201, f'Kirim: +{req.unique_amount} UZS')
        self.assertEqual(r['outcome'], 'held')
        user.refresh_from_db()
        self.assertEqual(user.balance, 0)  # NOT credited


class UniqueAmountTests(TestCase):
    def test_unique_amount_collision_avoided(self):
        from apps.cardpay import services as cs
        Setting.set_setting('payment_card_monitor_enabled', 'True')
        Setting.set_setting('payment_unique_offset_max', '0')
        Setting.clear_cache()
        user = make_user()
        tx1 = BalanceTransaction.objects.create(user=user, tx_type='topup', amount=10000,
                                                balance_before=0, balance_after=0, status='pending')
        tx2 = BalanceTransaction.objects.create(user=user, tx_type='topup', amount=10000,
                                                balance_before=0, balance_after=0, status='pending')
        req1 = cs.create_topup_request(user, tx1, Decimal('10000'))
        # offset 0 → the only candidate (10000) is taken → the guard must
        # REFUSE instead of handing out a duplicate that could credit the
        # wrong user. The caller (balance_views) falls back to admin approval.
        with self.assertRaises(ValueError):
            cs.create_topup_request(user, tx2, Decimal('10000'))
        self.assertEqual(CardTopupRequest.objects.filter(status='pending').count(), 1)

    def test_concurrent_same_payment_idempotent(self):
        from apps.cardpay import services as cs
        Setting.set_setting('payment_card_monitor_enabled', 'True')
        Setting.clear_cache()
        user = make_user()
        tx = BalanceTransaction.objects.create(user=user, tx_type='topup', amount=10000,
                                                balance_before=0, balance_after=0, status='pending')
        req = cs.create_topup_request(user, tx, Decimal('10000'))
        text = f'Kirim: +{req.unique_amount} UZS'
        r1 = cs.consume_payment_message('c', 101, text)
        r2 = cs.consume_payment_message('c', 101, text)  # same message id → dedup
        self.assertEqual(r1['outcome'], 'matched')
        self.assertEqual(r2['outcome'], 'duplicate')
        user.refresh_from_db()
        self.assertEqual(user.balance, req.unique_amount)
