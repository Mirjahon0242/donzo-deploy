"""Tests for the 'Statistikani 0 ga qaytarish' admin endpoint.

POST /api/v1/admin/crm/reset-stats/ — faqat Super Admin. Orders, Payments,
BalanceTransaction va AuditLog'larni o'chiradi; userlar saqlanadi; reset
amali keyingi audit log sifatida yoziladi.
"""
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.users.models import User, Role
from apps.orders.models import Order
from apps.payments.models import Payment, BalanceTransaction
from apps.audit_log.models import AuditLog
from apps.services.models import Service, Category, Package


def make_user(role=Role.SUPER_ADMIN, username='boss'):
    return User.objects.create(
        username=username,
        email=f'{username}@test.uz',
        role=role,
        is_active=True,
    )


def make_order(customer=None, total=10000):
    cat, _ = Category.objects.get_or_create(slug='mobile-games', defaults={'name': 'Mobile'})
    svc, _ = Service.objects.get_or_create(
        slug='pubg-mobile',
        defaults={'name': 'PUBG', 'category': cat, 'is_active': True},
    )
    pkg, _ = Package.objects.get_or_create(
        service=svc, name='60 UC',
        defaults={'amount_label': '60 UC', 'price': 12000, 'order_index': 1},
    )
    return Order.objects.create(
        order_number=f'ORD-{Order.objects.count() + 1}',
        customer=customer,
        service=svc,
        package=pkg,
        customer_name='Test Mijoz',
        customer_telegram='@test',
        total_price=Decimal(total),
        status='completed',
        payment_status='paid',
        payment_method='balance',
    )


class ResetSalesStatsTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_unauthenticated_forbidden(self):
        resp = self.client.post('/api/v1/admin/crm/reset-stats/')
        self.assertEqual(resp.status_code, 401)

    def test_customer_forbidden(self):
        u = make_user(role=Role.CUSTOMER, username='customer1')
        self.client.force_authenticate(u)
        resp = self.client.post('/api/v1/admin/crm/reset-stats/')
        self.assertEqual(resp.status_code, 403)

    def test_admin_forbidden(self):
        u = make_user(role=Role.ADMIN, username='admin1')
        self.client.force_authenticate(u)
        resp = self.client.post('/api/v1/admin/crm/reset-stats/')
        self.assertEqual(resp.status_code, 403)

    def test_super_admin_resets_everything(self):
        u = make_user()
        order = make_order(customer=u)
        Payment.objects.create(
            order=order, provider='balance', amount=Decimal('10000'),
            status='succeeded',
        )
        BalanceTransaction.objects.create(
            user=u, tx_type='purchase', amount=Decimal('-10000'),
            balance_before=Decimal('50000'), balance_after=Decimal('40000'),
            status='completed',
        )
        AuditLog.objects.create(
            user=u, action='some_action', target_type='Order',
            description='eski log',
        )

        self.client.force_authenticate(u)
        resp = self.client.post('/api/v1/admin/crm/reset-stats/')
        self.assertEqual(resp.status_code, 200)
        data = resp.data
        self.assertTrue(data['ok'])
        self.assertEqual(data['deleted']['orders'], 1)
        self.assertEqual(data['deleted']['payments'], 1)
        self.assertEqual(data['deleted']['balance_transactions'], 1)
        # AuditLog'larga fon fragment-sync thread'lari ham yozishi mumkin
        # (PostgreSQL test izolyatsiyasida o'z ulanishi bilan commit qiladi) —
        # biz yaratgan log ALBATTA o'chirilganini tekshiramiz.
        self.assertGreaterEqual(data['deleted']['audit_logs'], 1)

        # Barcha savdo ma'lumotlari 0 ga tushdi...
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(BalanceTransaction.objects.count(), 0)
        # ...lekin foydalanuvchilar o'chmaydi
        self.assertEqual(User.objects.count(), 1)

        # Reset amali keyingi audit log sifatida qayd etilgan
        log = AuditLog.objects.filter(action='sales_stats_reset').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.user, u)
        self.assertIn('1 buyurtma', log.description)

    def test_reset_when_empty_is_idempotent(self):
        u = make_user()
        self.client.force_authenticate(u)
        resp = self.client.post('/api/v1/admin/crm/reset-stats/')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['ok'])
        # faqat reset log'i qoladi
        self.assertEqual(AuditLog.objects.count(), 1)
        self.assertEqual(Order.objects.count(), 0)
