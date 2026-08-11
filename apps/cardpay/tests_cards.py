"""
PaymentCard registry tests — multi-card limits and auto-rotation (DONZO).

Covers:
  • create/list/activate/reset/delete via admin API
  • legacy single-card settings are seeded into the registry
  • register_card_payment counts usage on the card matching the message tail
  • auto-rotation when a card hits its max_amount / max_transfers
  • daily counter reset
  • get_settings returns the active card's number/holder
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.settings_app.models import Setting
from apps.cardpay.models import PaymentCard
from apps.cardpay import services

User = get_user_model()


class PaymentCardApiTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username='admin_cards', email='admin_cards@donzo.uz', password='x',
            role='super_admin',
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def _url(self, name, *args):
        return reverse(name, args=args)

    def test_create_first_card_is_auto_active(self):
        res = self.client.post('/api/v1/admin/cardpay/cards/', {
            'card_number': '8600123412341234',
            'card_holder': 'DONZO PAYMENT',
            'bank_name': 'Xalq Banki',
            'max_amount': 5_000_000,
            'max_transfers': 50,
        })
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(res.data['is_active'])
        self.assertEqual(res.data['card_tail'], '1234')

    def test_create_duplicate_card_rejected(self):
        PaymentCard.objects.create(card_number='8600123412341234', is_active=True)
        res = self.client.post('/api/v1/admin/cardpay/cards/', {'card_number': '8600123412341234'})
        self.assertEqual(res.status_code, 400)

    def test_activate_switches_active_card(self):
        c1 = PaymentCard.objects.create(card_number='8600111111111111', is_active=True)
        c2 = PaymentCard.objects.create(card_number='8600222222222222')
        res = self.client.post(f'/api/v1/admin/cardpay/cards/{c2.id}/activate/')
        self.assertEqual(res.status_code, 200)
        c1.refresh_from_db(); c2.refresh_from_db()
        self.assertFalse(c1.is_active)
        self.assertTrue(c2.is_active)

    def test_delete_active_card_activates_next(self):
        c1 = PaymentCard.objects.create(card_number='8600111111111111', is_active=True)
        c2 = PaymentCard.objects.create(card_number='8600222222222222', enabled=True)
        res = self.client.delete(f'/api/v1/admin/cardpay/cards/{c1.id}/')
        self.assertEqual(res.status_code, 200)
        c2.refresh_from_db()
        self.assertTrue(c2.is_active)

    def test_reset_counters(self):
        c = PaymentCard.objects.create(
            card_number='8600111111111111', is_active=True,
            total_amount=Decimal('100000'), transfers_count=12,
        )
        res = self.client.post(f'/api/v1/admin/cardpay/cards/{c.id}/reset/')
        self.assertEqual(res.status_code, 200)
        c.refresh_from_db()
        self.assertEqual(c.total_amount, 0)
        self.assertEqual(c.transfers_count, 0)

    def test_list_returns_usage_percent(self):
        PaymentCard.objects.create(
            card_number='8600111111111111', is_active=True,
            max_amount=Decimal('1000000'), total_amount=Decimal('250000'),
            max_transfers=10, transfers_count=5,
        )
        res = self.client.get('/api/v1/admin/cardpay/cards/')
        self.assertEqual(res.status_code, 200)
        card = res.data['cards'][0]
        self.assertEqual(card['amount_usage_pct'], 25.0)
        self.assertEqual(card['transfer_usage_pct'], 50.0)
        self.assertFalse(card['is_exhausted'])
        self.assertIsNotNone(res.data['active_card'])


class PaymentCardRotationTests(TestCase):
    def setUp(self):
        self.c1 = PaymentCard.objects.create(
            card_number='8600111111111111', is_active=True,
            max_amount=Decimal('100000'), max_transfers=10, order_index=1,
        )
        self.c2 = PaymentCard.objects.create(
            card_number='8600222222222222', enabled=True,
            max_amount=Decimal('500000'), max_transfers=100, order_index=2,
        )

    def test_register_counts_on_tail_matched_card(self):
        res = services.register_card_payment('💳 ***1111\n➕ 50 000 UZS', Decimal('50000'))
        self.assertEqual(res['card_tail'], '1111')
        self.c1.refresh_from_db()
        self.assertEqual(self.c1.total_amount, Decimal('50000'))
        self.assertEqual(self.c1.transfers_count, 1)

    def test_amount_limit_triggers_rotation(self):
        services.register_card_payment('💳 ***1111', Decimal('60000'))
        services.register_card_payment('💳 ***1111', Decimal('50000'))  # 110k > 100k limit
        self.c1.refresh_from_db(); self.c2.refresh_from_db()
        self.assertFalse(self.c1.is_active)
        self.assertTrue(self.c2.is_active)
        self.assertEqual(self.c1.transfers_count, 2)

    def test_transfer_limit_triggers_rotation(self):
        # max_transfers=10 → the 10th transfer rotates
        for i in range(10):
            services.register_card_payment('💳 ***1111', Decimal('1000'))
        self.c1.refresh_from_db(); self.c2.refresh_from_db()
        self.assertFalse(self.c1.is_active)
        self.assertTrue(self.c2.is_active)

    def test_no_rotation_before_limit(self):
        for i in range(9):
            services.register_card_payment('💳 ***1111', Decimal('1000'))
        self.c1.refresh_from_db(); self.c2.refresh_from_db()
        self.assertTrue(self.c1.is_active)
        self.assertFalse(self.c2.is_active)

    def test_daily_reset_restores_card(self):
        # Exhaust c1, rotate to c2
        services.register_card_payment('💳 ***1111', Decimal('150000'))
        self.c1.refresh_from_db()
        self.assertFalse(self.c1.is_active)
        # New day → counters reset
        self.c1.period_started_at = timezone.now() - timezone.timedelta(days=1, hours=2)
        self.c1.total_amount = Decimal('150000')
        self.c1.transfers_count = 1
        self.c1.save()
        services.register_card_payment('💳 ***2222', Decimal('1000'))
        self.c1.refresh_from_db()
        self.assertEqual(self.c1.total_amount, 0)
        self.assertEqual(self.c1.transfers_count, 0)

    def test_get_settings_uses_active_card(self):
        Setting.set_setting('payment_card_number', '8600999999999999')
        Setting.set_setting('payment_card_holder', 'LEGACY')
        s = services.get_settings()
        self.assertEqual(s['card_number'], '8600111111111111')
        self.assertEqual(s['card_holder'], '')

    def test_legacy_settings_seed_active_card(self):
        # Fresh DB state (no cards) → legacy settings seed one
        PaymentCard.objects.all().delete()
        Setting.set_setting('payment_card_number', '8600999999999999')
        Setting.set_setting('payment_card_holder', 'LEGACY HOLDER')
        card = services.get_active_card()
        self.assertIsNotNone(card)
        self.assertEqual(card.card_number, '8600999999999999')
        self.assertTrue(card.is_active)

    def test_all_cards_exhausted_keeps_current(self):
        # c2 already exhausted too → no candidate left, c1 stays active
        self.c2.max_amount = Decimal('50000')
        self.c2.total_amount = Decimal('60000')
        self.c2.save()
        services.register_card_payment('💳 ***1111', Decimal('150000'))
        self.c1.refresh_from_db()
        self.assertTrue(self.c1.is_active)  # stays active, staff alerted
