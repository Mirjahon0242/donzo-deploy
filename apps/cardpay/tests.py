"""
Tests for the card payment auto-verification (DONZO).
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.payments.models import BalanceTransaction
from apps.settings_app.models import Setting
from apps.users.permissions import IsAdmin  # noqa: F401 (role import anchor)
from apps.users import telegram_notify
from . import services, user_client_auth
from .models import CardPaymentMessage, CardTopupRequest, SuspiciousPayment, parse_amounts_from_text

User = get_user_model()


class StaffSuspiciousNotifyTests(TestCase):
    """Direct staff Telegram alerts for suspicious payments (report group
    bundan tashqari) — throttled, staff-only, with inline buttons."""

    def setUp(self):
        Setting.set_setting('payment_card_monitor_enabled', 'True')
        Setting.set_setting('payment_suspicious_limit', '100000')
        Setting.set_setting('telegram_bot_token', '123456:TEST-TOKEN')
        Setting.clear_cache()
        self.user = User.objects.create_user(
            username='sp_cust', email='sp_cust@tg.user', telegram_id='9001',
            telegram_username='sp_cust',
        )
        self.staff_admin = User.objects.create_user(
            username='sp_admin', email='sp_admin@tg.user', role='super_admin',
            telegram_id='7001', telegram_username='sp_admin',
        )
        self.staff_operator = User.objects.create_user(
            username='sp_operator', email='sp_operator@tg.user', role='operator',
            telegram_id='7002', telegram_username='sp_operator',
        )
        # Customer must NOT receive staff alerts
        self.other_customer = User.objects.create_user(
            username='sp_other', email='sp_other@tg.user', telegram_id='7003',
        )

    def _make_suspicious(self):
        tx = BalanceTransaction.objects.create(
            user=self.user, tx_type='topup', amount=200000,
            balance_before=0, balance_after=0, status='pending',
        )
        req = services.create_topup_request(self.user, tx, Decimal('200000'))
        res = services.consume_payment_message('c', 11, f'Kirim: +{req.unique_amount} UZS')
        return SuspiciousPayment.objects.get(pk=res['suspicious_id'])

    def test_suspicious_consumption_notifies_staff(self):
        """Suspicious payment → direct staff alerts (report group + staff)."""
        with mock.patch.object(telegram_notify, '_send_message', return_value=True) as send:
            sp = self._make_suspicious()
        self.assertIsNotNone(sp)
        # Only the two staff chats (admin + operator), never the customer.
        # _send_message(bot_token, chat_id, text, ...) → chat_id is args[1].
        sent_to = {call.args[1] for call in send.call_args_list}
        self.assertEqual(sent_to, {'7001', '7002'})
        # Staff notification carries Approve/Reject buttons (sp: callback)
        staff_calls = [c for c in send.call_args_list if c.args[1] in ('7001', '7002')]
        for call in staff_calls:
            kb = call.kwargs.get('reply_markup') or {}
            flat = [b['callback_data'] for row in kb.get('inline_keyboard', []) for b in row]
            self.assertEqual(flat, [f'sp:{sp.id}:approve', f'sp:{sp.id}:reject'])

    def test_staff_alert_throttled_per_payment(self):
        """The same suspicious payment never re-alerts staff."""
        with mock.patch.object(telegram_notify, '_send_message', return_value=True):
            sp = self._make_suspicious()
        with mock.patch.object(telegram_notify, '_send_message', return_value=True) as send:
            n = telegram_notify.notify_staff_suspicious_payment(sp)
        self.assertEqual(n, 0)
        send.assert_not_called()

    def test_customer_never_gets_staff_alert(self):
        """A customer-only DB (no staff) → zero alerts, no crash."""
        from apps.users.models import User
        User.objects.filter(role__in=telegram_notify.NOTIFY_ROLES).delete()
        with mock.patch.object(telegram_notify, '_send_message', return_value=True) as send:
            sp = self._make_suspicious()
        self.assertIsNotNone(sp)
        send.assert_not_called()

    def test_approve_callback_credits_and_reject_blocks(self):
        """Direct approve/reject via services (what the sp: buttons call)."""
        with mock.patch.object(telegram_notify, '_send_message', return_value=True):
            sp = self._make_suspicious()
            out = services.approve_suspicious(sp.id, self.staff_admin)
            self.assertTrue(out['ok'])
            self.user.refresh_from_db()
            self.assertEqual(self.user.balance, sp.amount)
            sp.refresh_from_db()
            self.assertEqual(sp.status, 'approved')

            # New suspicious payment → reject leaves balance untouched.
            # Use a different requested amount so its unique_amount cannot
            # collide with the first request's (already-paid) one.
            tx2 = BalanceTransaction.objects.create(
                user=self.user, tx_type='topup', amount=300000,
                balance_before=0, balance_after=0, status='pending',
            )
            req2 = services.create_topup_request(self.user, tx2, Decimal('300000'))
            res2 = services.consume_payment_message('c', 12, f'Kirim: +{req2.unique_amount} UZS')
            self.assertEqual(res2['outcome'], 'suspicious')
            sp2 = SuspiciousPayment.objects.get(pk=res2['suspicious_id'])
            out2 = services.reject_suspicious(sp2.id, self.staff_admin, 'test rad')
            self.assertTrue(out2['ok'])
            sp2.refresh_from_db()
            self.assertEqual(sp2.status, 'rejected')
            self.user.refresh_from_db()
            self.assertEqual(self.user.balance, sp.amount)  # unchanged by reject

    def test_no_token_graceful(self):
        Setting.set_setting('telegram_bot_token', '')
        Setting.clear_cache()
        with mock.patch.object(telegram_notify, '_send_message', return_value=True) as send:
            sp = self._make_suspicious()
        self.assertIsNotNone(sp)
        send.assert_not_called()



class ParserTests(TestCase):
    def setUp(self):
        Setting.set_setting('payment_card_monitor_enabled', 'True')
        Setting.clear_cache()

    def test_separated_and_bare_amounts(self):
        self.assertEqual(parse_amounts_from_text('Kirim: +5 001 UZS'), [5001])
        self.assertEqual(parse_amounts_from_text('+125000.00 UZS'), [125000])
        self.assertEqual(parse_amounts_from_text('1,250,000 so\'m'), [1250000])

    def test_cardxabar_bot_format(self):
        """Real @CardXabarBot format — space thousands + '.00' decimal."""
        text = (
            "🟢 Perevod na kartu\n"
            "➕ 1 061.00 UZS\n"
            "💳 ***2917\n"
            "📍 TOSHKENT SH., AT KHALK BANKI BOSH AMALIY, UZ\n"
            "🕓 09.08.26 20:35\n"
            "💵 1 061.00 UZS"
        )
        res = parse_amounts_from_text(text)
        # 1 061.00 → 1061, never 106100; date/card never become amounts
        self.assertIn(1061, res)
        self.assertNotIn(106100, res)
        self.assertNotIn(2917, res)
        self.assertNotIn(2026, res)

    def test_years_are_filtered(self):
        res = parse_amounts_from_text('06.08.2026 18:32 +5001')
        self.assertNotIn(2026, res)
        self.assertIn(5001, res)

    def test_outgoing_detected_by_matcher_absence(self):
        # No pending request → no_match regardless of parsing
        res = services.consume_payment_message('c', 1, 'Chiqim: 10 000 so\'m')
        self.assertEqual(res['outcome'], 'no_match')


class ServicesTests(TestCase):
    def setUp(self):
        Setting.set_setting('payment_card_monitor_enabled', 'True')
        Setting.set_setting('payment_suspicious_limit', '500000')
        Setting.set_setting('payment_timeout_minutes', '10')
        Setting.set_setting('payment_unique_offset_max', '999')
        Setting.clear_cache()
        self.user = User.objects.create_user(
            username='card_user', email='card@tg.user',
            telegram_id='1001', telegram_username='card_user',
        )
        self.tx = BalanceTransaction.objects.create(
            user=self.user, tx_type='topup', amount=5000,
            balance_before=0, balance_after=0, status='pending',
        )

    def test_unique_amount_in_range(self):
        req = services.create_topup_request(self.user, self.tx, Decimal('5000'))
        self.assertTrue(Decimal('5000') <= req.unique_amount <= Decimal('5999'))
        self.assertEqual(req.status, 'pending')

    def test_payment_credits_balance(self):
        req = services.create_topup_request(self.user, self.tx, Decimal('5000'))
        res = services.consume_payment_message('c', 1, f'Kirim: +{req.unique_amount} UZS')
        self.assertEqual(res['outcome'], 'matched')
        req.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(req.status, 'paid')
        self.assertEqual(self.user.balance, req.unique_amount)
        self.tx.refresh_from_db()
        self.assertEqual(self.tx.status, 'completed')

    def test_cardxabar_bot_message_credits_automatically(self):
        """Real @CardXabarBot message matching a pending unique_amount credits."""
        req = CardTopupRequest.objects.create(
            user=self.user, balance_tx=self.tx, requested_amount=Decimal('1000'),
            unique_amount=Decimal('1061'),
            expires_at=timezone.now() + timedelta(minutes=10),
            status='pending',
        )
        text = (
            "🟢 Perevod na kartu\n"
            "➕ 1 061.00 UZS\n"
            "💳 ***2917\n"
            "📍 TOSHKENT SH., AT KHALK BANKI BOSH AMALIY, UZ\n"
            "🕓 09.08.26 20:35\n"
            "💵 1 061.00 UZS"
        )
        res = services.consume_payment_message('c', 21, text)
        self.assertEqual(res['outcome'], 'matched')
        req.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(req.status, 'paid')
        self.assertEqual(self.user.balance, Decimal('1061'))
        self.tx.refresh_from_db()
        self.assertEqual(self.tx.status, 'completed')

    def test_message_dedup(self):
        req = services.create_topup_request(self.user, self.tx, Decimal('5000'))
        text = f'Kirim: +{req.unique_amount} UZS'
        first = services.consume_payment_message('c', 7, text)
        second = services.consume_payment_message('c', 7, text)
        self.assertEqual(first['outcome'], 'matched')
        self.assertEqual(second['outcome'], 'duplicate')
        self.user.refresh_from_db()
        # Credited exactly once
        self.assertEqual(self.user.balance, req.unique_amount)

    def test_wrong_amount_no_match(self):
        services.create_topup_request(self.user, self.tx, Decimal('5000'))
        res = services.consume_payment_message('c', 2, 'Kirim: +12345 UZS')
        self.assertEqual(res['outcome'], 'no_match')
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, 0)

    def test_suspicious_not_credited(self):
        Setting.set_setting('payment_suspicious_limit', '100000')
        Setting.clear_cache()
        req = services.create_topup_request(self.user, self.tx, Decimal('200000'))
        self.assertGreater(req.unique_amount, 100000)
        res = services.consume_payment_message('c', 3, f'Kirim: +{req.unique_amount} UZS')
        self.assertEqual(res['outcome'], 'suspicious')
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, 0)  # NOT credited
        self.assertTrue(SuspiciousPayment.objects.filter(status='pending').exists())

    def test_suspicious_approve_credits(self):
        Setting.set_setting('payment_suspicious_limit', '100000')
        Setting.clear_cache()
        req = services.create_topup_request(self.user, self.tx, Decimal('200000'))
        res = services.consume_payment_message('c', 4, f'Kirim: +{req.unique_amount} UZS')
        sp = SuspiciousPayment.objects.get(pk=res['suspicious_id'])
        admin = User.objects.create_user(
            username='boss', email='boss@tg.user', role='super_admin',
        )
        out = services.approve_suspicious(sp.id, admin)
        self.assertTrue(out['ok'])
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, req.unique_amount)
        sp.refresh_from_db()
        self.assertEqual(sp.status, 'approved')

    def test_duplicate_suspicious_never_double_credits(self):
        """Bank can send two notifications for one transfer → two suspicious
        rows for the same request. Approving both must credit only ONCE."""
        Setting.set_setting('payment_suspicious_limit', '100000')
        Setting.clear_cache()
        req = services.create_topup_request(self.user, self.tx, Decimal('200000'))
        text = f'Kirim: +{req.unique_amount} UZS'
        # Two DIFFERENT message ids (duplicate bank SMS for one transfer)
        r1 = services.consume_payment_message('c', 41, text)
        r2 = services.consume_payment_message('c', 42, text)
        self.assertEqual(r1['outcome'], 'suspicious')
        self.assertEqual(r2['outcome'], 'suspicious')
        self.assertEqual(SuspiciousPayment.objects.filter(status='pending').count(), 2)

        admin = User.objects.create_user(
            username='boss2', email='boss2@tg.user', role='super_admin',
        )
        out1 = services.approve_suspicious(r1['suspicious_id'], admin)
        self.assertTrue(out1['ok'])
        self.user.refresh_from_db()
        first_credit = self.user.balance
        self.assertEqual(first_credit, req.unique_amount)

        # Second approval must be refused — NO extra credit
        out2 = services.approve_suspicious(r2['suspicious_id'], admin)
        self.assertFalse(out2['ok'])
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, first_credit)
        # The second suspicious row stays pending so the admin sees it
        sp2 = SuspiciousPayment.objects.get(pk=r2['suspicious_id'])
        self.assertEqual(sp2.status, 'pending')

    def test_expire_stale(self):
        req = services.create_topup_request(self.user, self.tx, Decimal('5000'))
        req.expires_at = timezone.now() - timedelta(minutes=1)
        req.save(update_fields=['expires_at'])
        n = services.expire_stale_requests()
        self.assertEqual(n, 1)
        req.refresh_from_db()
        self.assertEqual(req.status, 'expired')
        self.tx.refresh_from_db()
        self.assertEqual(self.tx.status, 'cancelled')

    def test_paid_request_cannot_be_credited_twice(self):
        req = services.create_topup_request(self.user, self.tx, Decimal('5000'))
        services.consume_payment_message('c', 5, f'Kirim: +{req.unique_amount} UZS')
        # A second message with the same amount — no pending request left
        res = services.consume_payment_message('c', 6, f'Kirim: +{req.unique_amount} UZS')
        self.assertEqual(res['outcome'], 'no_match')


class CardpayAdminAPITests(TestCase):
    def setUp(self):
        Setting.set_setting('payment_card_monitor_enabled', 'True')
        Setting.clear_cache()
        self.admin = User.objects.create_user(
            username='admin', email='admin@tg.user', role='super_admin',
            telegram_id='2007554600',
        )
        self.user = User.objects.create_user(
            username='cust', email='cust@tg.user', telegram_id='2001',
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def _make_request(self):
        tx = BalanceTransaction.objects.create(
            user=self.user, tx_type='topup', amount=5000,
            balance_before=0, balance_after=0, status='pending',
        )
        return services.create_topup_request(self.user, tx, Decimal('5000'))

    def test_settings_get_put(self):
        url = '/api/v1/admin/cardpay/settings/'
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertIn('monitor_chat_id', r.data)
        r = self.client.put(url, {'payment_monitor_chat_id': '-1001234567890',
                                  'payment_suspicious_limit': '250000'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['monitor_chat_id'], '-1001234567890')
        self.assertEqual(r.data['suspicious_limit'], 250000)

    def test_requests_list_and_statuses(self):
        req = self._make_request()
        r = self.client.get('/api/v1/admin/cardpay/requests/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('pending', r.data['counts'])
        ids = [x['id'] for x in r.data['results']]
        self.assertIn(req.id, ids)

    def test_suspicious_list_and_approve(self):
        tx = BalanceTransaction.objects.create(
            user=self.user, tx_type='topup', amount=600000,
            balance_before=0, balance_after=0, status='pending',
        )
        req = services.create_topup_request(self.user, tx, Decimal('600000'))
        res = services.consume_payment_message('c', 9, f'Kirim: +{req.unique_amount} UZS')
        sp_id = res['suspicious_id']

        r = self.client.get('/api/v1/admin/cardpay/suspicious/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['counts']['pending'], 1)

        r = self.client.post(f'/api/v1/admin/cardpay/suspicious/{sp_id}/approve/')
        self.assertEqual(r.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, req.unique_amount)

    def test_unauthorized_401(self):
        anon = APIClient()
        r = anon.get('/api/v1/admin/cardpay/settings/')
        self.assertEqual(r.status_code, 401)

    def test_status_endpoint(self):
        self._make_request()
        r = self.client.get('/api/v1/admin/cardpay/status/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('today', r.data)


class CardpayIntegrityTests(TestCase):
    """Full-flow integrity: duplicate bank messages, expired approvals,
    offset-space exhaustion — money must never be double-credited and must
    never be silently lost."""

    def setUp(self):
        Setting.set_setting('payment_card_monitor_enabled', 'True')
        Setting.set_setting('payment_suspicious_limit', '500000')
        Setting.set_setting('payment_timeout_minutes', '10')
        Setting.set_setting('payment_unique_offset_max', '999')
        Setting.clear_cache()
        self.user = User.objects.create_user(
            username='integrity_user', email='integrity@tg.user',
            telegram_id='3001', telegram_username='integrity_user',
        )
        self.admin = User.objects.create_user(
            username='integrity_admin', email='iadmin@tg.user',
            role='super_admin', telegram_id='3002',
        )

    def _request(self, amount=5000):
        tx = BalanceTransaction.objects.create(
            user=self.user, tx_type='topup', amount=amount,
            balance_before=0, balance_after=0, status='pending',
        )
        return services.create_topup_request(self.user, tx, Decimal(amount))

    def test_bank_double_message_never_double_credits(self):
        """Bank sends TWO DIFFERENT messages for one transfer (same amount):
        exactly one credit — the second message finds no pending request."""
        req = self._request()
        text = f'Kirim: +{req.unique_amount} UZS'
        r1 = services.consume_payment_message('c', 501, text)
        r2 = services.consume_payment_message('c', 502, text)  # different msg id
        self.assertEqual(r1['outcome'], 'matched')
        self.assertEqual(r2['outcome'], 'no_match')  # request already paid
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, req.unique_amount)  # credited ONCE
        req.refresh_from_db()
        self.assertEqual(req.status, 'paid')
        self.assertEqual(CardPaymentMessage.objects.filter(chat_id='c').count(), 2)

    def test_expired_request_admin_approve_credits(self):
        """A suspicious transfer whose request EXPIRED before the admin
        decided: approving it still credits the balance (money arrived) and
        the linked tx is reopened to completed."""
        Setting.set_setting('payment_suspicious_limit', '100000')
        Setting.clear_cache()
        req = self._request(amount=200000)
        res = services.consume_payment_message('c', 601, f'Kirim: +{req.unique_amount} UZS')
        self.assertEqual(res['outcome'], 'suspicious')
        sp = SuspiciousPayment.objects.get(pk=res['suspicious_id'])
        # Let the request expire, then sweep (also cancels its pending tx)
        req.expires_at = timezone.now() - timedelta(minutes=1)
        req.save(update_fields=['expires_at'])
        n = services.expire_stale_requests()
        self.assertEqual(n, 1)
        req.refresh_from_db()
        self.assertEqual(req.status, 'expired')
        self.assertEqual(req.balance_tx.status, 'cancelled')
        # Admin approves the expired payment → balance credited
        out = services.approve_suspicious(sp.id, self.admin)
        self.assertTrue(out['ok'])
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, sp.amount)
        sp.refresh_from_db()
        self.assertEqual(sp.status, 'approved')
        req.balance_tx.refresh_from_db()
        self.assertEqual(req.balance_tx.status, 'completed')

    def test_incident_approve_credits_expired_request(self):
        """Regression: a HOLD incident whose request EXPIRED while the admin
        decided must still credit on approve (credit_request allow_expired).
        Before the fix the admin saw 'approved' but no money moved."""
        Setting.set_setting('security_ai_enabled', 'False')
        Setting.set_setting('security_shadow_mode', 'False')
        Setting.set_setting('risk_medium_max', '30')
        Setting.clear_cache()
        from apps.security import services as sec_services
        from apps.security.models import SecurityIncident

        risky = User.objects.create_user(
            username='risky_int', email='risky_int@tg.user', telegram_id='3003',
            date_joined=timezone.now() - timedelta(hours=1),
        )
        tx = BalanceTransaction.objects.create(
            user=risky, tx_type='topup', amount=400000,
            balance_before=0, balance_after=0, status='pending',
        )
        req = services.create_topup_request(risky, tx, Decimal('400000'))
        out = sec_services.evaluate_payment(risky, Decimal('400000'), request=req)
        self.assertEqual(out['decision'], 'HOLD')
        inc_id = out['incident_id']
        # Request expires before the admin acts
        req.expires_at = timezone.now() - timedelta(minutes=1)
        req.save(update_fields=['expires_at'])
        services.expire_stale_requests()
        req.refresh_from_db()
        self.assertEqual(req.status, 'expired')
        # Admin approves → money must move despite the expiry. The credited
        # amount is the UNIQUE amount the user was told to send (requested +
        # random offset).
        result = sec_services.resolve_incident(inc_id, self.admin, 'approve')
        self.assertTrue(result['ok'])
        self.assertTrue(result['credited'])
        req.refresh_from_db()
        risky.refresh_from_db()
        self.assertEqual(risky.balance, req.unique_amount)
        inc = SecurityIncident.objects.get(pk=inc_id)
        self.assertEqual(inc.status, 'RESOLVED')
        # Second approve is refused (never double-credit)
        result2 = sec_services.resolve_incident(inc_id, self.admin, 'approve')
        self.assertFalse(result2['ok'])
        risky.refresh_from_db()
        self.assertEqual(risky.balance, req.unique_amount)

    def test_offset_exhausted_falls_back_to_admin_approval(self):
        """When the unique-offset space is exhausted (offset 0 + same nominal
        already pending), the API falls back to the classic admin-approval
        flow instead of handing out a duplicate amount that could credit the
        wrong user."""
        Setting.set_setting('payment_unique_offset_max', '0')
        Setting.clear_cache()
        client = APIClient()
        client.force_authenticate(self.user)
        # NOTE: real mount is /payments/balance/topup/ (see api.ts)
        r1 = client.post('/api/v1/payments/balance/topup/',
                         {'amount': '10000', 'idempotency_key': 'k1'}, format='json')
        self.assertEqual(r1.status_code, 200)
        self.assertFalse(r1.data['requires_approval'])  # card flow active
        # Second identical top-up → offset space taken → admin-approval fallback
        r2 = client.post('/api/v1/payments/balance/topup/',
                         {'amount': '10000', 'idempotency_key': 'k2'}, format='json')
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.data['requires_approval'])
        self.assertFalse(r2.data['requires_unique_payment'])
        self.assertNotIn('card_request_id', r2.data)
        # No duplicate pending card request was handed out
        self.assertEqual(CardTopupRequest.objects.filter(status='pending').count(), 1)
        # The fallback request is still approvable by an admin → balance moves
        tx2 = BalanceTransaction.objects.get(pk=r2.data['balance_tx_id'])
        self.assertEqual(tx2.status, 'pending')
        admin_client = APIClient()
        admin_client.force_authenticate(self.admin)
        ra = admin_client.post(f'/api/v1/admin/balance-topups/{tx2.id}/approve/')
        self.assertEqual(ra.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, Decimal('10000'))


class UserClientAuthTests(TestCase):
    """Regression: verify_code must never raise UnboundLocalError/500.

    The original code forgot `_PHONE_CODE_HASH` in the `global` statement,
    so Python treated it as a local variable and every code verification
    crashed with a 500 ("xatolik" toast in the admin panel).
    """

    def setUp(self):
        user_client_auth._clear_login_state()

    def test_verify_code_without_phone_returns_clear_error(self):
        r = user_client_auth.verify_code('12345')
        self.assertFalse(r['ok'])
        self.assertIn('raqam', r['detail'])

    def test_verify_code_without_code_returns_clear_error(self):
        user_client_auth._set_login_state('+998901234567', 'dummyhash', False)
        r = user_client_auth.verify_code('')
        self.assertFalse(r['ok'])
        self.assertIn('Kod', r['detail'])

    def test_verify_code_validates_bad_code_without_crash(self):
        # With a phone set but no real Telegram session, sign_in must fail
        # gracefully with an error dict — not raise UnboundLocalError.
        user_client_auth._set_login_state('+998901234567', 'dummyhash', False)
        r = user_client_auth.verify_code('00000')
        self.assertFalse(r['ok'])
        self.assertIn('detail', r)

    def test_verify_password_without_2fa_step_returns_clear_error(self):
        r = user_client_auth.verify_password('secret')
        self.assertFalse(r['ok'])
        self.assertIn('Parol', r['detail'])

    def test_login_state_is_db_backed_cross_worker(self):
        """Regression: login state must survive across daphne worker
        processes (in-memory globals are per-process → "kod topilmadi")."""
        user_client_auth._set_login_state('+998901234567', 'hash123', True)
        phone, code_hash, needs_2fa = user_client_auth._get_login_state()
        self.assertEqual(phone, '+998901234567')
        self.assertEqual(code_hash, 'hash123')
        self.assertTrue(needs_2fa)
        # Simulate a different worker process: in-memory globals reset
        user_client_auth._PHONE = ''
        user_client_auth._PHONE_CODE_HASH = ''
        user_client_auth._NEEDS_PASSWORD = False
        phone2, code_hash2, needs_2fa2 = user_client_auth._get_login_state()
        self.assertEqual(phone2, '+998901234567')
        self.assertEqual(code_hash2, 'hash123')
        self.assertTrue(needs_2fa2)
        user_client_auth._clear_login_state()
        self.assertEqual(user_client_auth._get_login_state(), ('', '', False))
