"""Tests for the admin Telegram orders section (confirm/reject + Fragment API).

Oqim:
  • To'lov o'tgan Telegram Premium/Stars buyurtmasi 'pending' holatida admin
    tasdig'ini kutadi (auto-fulfillment o'chirilgan).
  • 'Tasdiqlash' -> fragment_api.buy_* chaqiriladi -> completed (yoki xatoda
    processing).
  • 'Rad qilish' -> buyurtma bekor qilinadi, to'langan balans qaytariladi.
"""

from decimal import Decimal
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from apps.users.models import User, Role
from apps.services.models import Service, Category, Package, ServiceField
from apps.orders.models import Order
from apps.payments.models import BalanceTransaction
from apps.audit_log.models import AuditLog
from apps.settings_app.models import Setting


def make_telegram_service():
    cat, _ = Category.objects.get_or_create(slug='social', defaults={'name': 'Social'})
    svc, _ = Service.objects.get_or_create(
        slug='telegram-premium',
        defaults={'name': 'Telegram Premium', 'category': cat, 'is_active': True},
    )
    svc.category = cat
    svc.is_active = True
    svc.save()
    ServiceField.objects.get_or_create(
        service=svc, field_name='username',
        defaults={'field_label': 'Telegram Username', 'field_type': 'text',
                  'is_required': True, 'order_index': 1},
    )
    for name, price in [('3 oy Premium', 125000), ('100 Stars', 23000)]:
        Package.objects.get_or_create(
            service=svc, name=name,
            defaults={'amount_label': name, 'price': price},
        )
    return svc


class TelegramAdminTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        Setting.set_setting('fragment_api_key', 'test-key-123')
        Setting.clear_cache()
        self.svc = make_telegram_service()
        self.premium_pkg = Package.objects.get(service=self.svc, name='3 oy Premium')
        self.stars_pkg = Package.objects.get(service=self.svc, name='100 Stars')
        self.admin = User.objects.create(
            username='admin1', email='admin1@tg.user', telegram_id='9001',
            telegram_username='admin1', role=Role.ADMIN,
        )
        self.operator = User.objects.create(
            username='op1', email='op1@tg.user', telegram_id='9002',
            telegram_username='op1', role=Role.OPERATOR,
        )
        self.customer = User.objects.create(
            username='cust1', email='cust1@tg.user', telegram_id='9003',
            telegram_username='cust_tg', first_name='Cust', role=Role.CUSTOMER,
            balance=Decimal('500000'),
        )

    def _make_paid_order(self, pkg=None, username='@durov'):
        pkg = pkg or self.premium_pkg
        return Order.objects.create(
            customer=self.customer,
            service=self.svc,
            package=pkg,
            field_values={'username': username},
            customer_name='Cust',
            customer_telegram='@cust_tg',
            total_price=pkg.price,
            status='pending',
            payment_status='paid',
            payment_method='balance',
        )

    # ── Ro'yxat ──

    def test_list_requires_admin(self):
        self.client.force_authenticate(self.operator)
        resp = self.client.get('/api/v1/admin/telegram-orders/')
        self.assertEqual(resp.status_code, 403, resp.content)
        self.client.force_authenticate(self.customer)
        resp = self.client.get('/api/v1/admin/telegram-orders/')
        self.assertEqual(resp.status_code, 403)

    def test_list_only_telegram_orders_and_stats(self):
        self._make_paid_order()
        self._make_paid_order(self.stars_pkg)
        # Telegram bo'lmagan buyurtma ro'yxatga kirmasligi kerak
        cat, _ = Category.objects.get_or_create(slug='mobile-games', defaults={'name': 'M'})
        other, _ = Service.objects.get_or_create(
            slug='pubg-mobile', defaults={'name': 'PUBG', 'category': cat})
        pkg, _ = Package.objects.get_or_create(service=other, name='60 UC', defaults={'price': 12000})
        Order.objects.create(customer=self.customer, service=other, package=pkg,
                             field_values={'game_id': '123'}, customer_name='C',
                             customer_telegram='@c', total_price=12000,
                             payment_status='paid', status='pending')

        self.client.force_authenticate(self.admin)
        resp = self.client.get('/api/v1/admin/telegram-orders/')
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()
        self.assertEqual(data['count'], 2)
        self.assertEqual(data['stats']['waiting'], 2)
        self.assertEqual(data['stats']['completed'], 0)
        self.assertEqual(data['stats']['total_revenue'], 0)

    # ── Tasdiqlash ──

    def test_confirm_success_completes_order(self):
        order = self._make_paid_order()
        self.client.force_authenticate(self.admin)
        with mock.patch(
            'apps.services.fragment_api.buy_premium',
            return_value={'username': '@durov', 'duration': 3,
                          'payment_method': 'TON', 'cost': '5'},
        ) as bp:
            resp = self.client.post(
                f'/api/v1/admin/telegram-orders/{order.id}/confirm/', {}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()
        self.assertTrue(data['ok'])
        order.refresh_from_db()
        self.assertEqual(order.status, 'completed')
        # double-spend himoyasi belgisi yozildi
        self.assertEqual(order.field_values.get('_fragment_attempted'), '1')
        bp.assert_called_once_with('@durov', 3)
        # audit log admin tomonidan yozildi (actor)
        self.assertTrue(AuditLog.objects.filter(
            action='auto_fulfillment', user=self.admin, target_id=order.id).exists())

    def test_confirm_fragment_error_marks_processing(self):
        from apps.services.fragment_api import FragmentAPIError
        order = self._make_paid_order()
        self.client.force_authenticate(self.admin)
        with mock.patch(
            'apps.services.fragment_api.buy_premium',
            side_effect=FragmentAPIError('wallet empty', error_code='INSUFFICIENT_FUNDS'),
        ):
            resp = self.client.post(
                f'/api/v1/admin/telegram-orders/{order.id}/confirm/', {}, format='json')
        data = resp.json()
        self.assertFalse(data['ok'])
        order.refresh_from_db()
        self.assertEqual(order.status, 'processing')

    def test_confirm_stars_uses_buy_stars(self):
        order = self._make_paid_order(self.stars_pkg)
        self.client.force_authenticate(self.admin)
        with mock.patch(
            'apps.services.fragment_api.buy_stars',
            return_value={'username': '@durov', 'amount': 100,
                          'payment_method': 'USDT', 'cost': '23'},
        ) as bs:
            resp = self.client.post(
                f'/api/v1/admin/telegram-orders/{order.id}/confirm/', {}, format='json')
        self.assertTrue(resp.json()['ok'])
        bs.assert_called_once_with('@durov', 100)

    def test_confirm_completed_order_400(self):
        order = self._make_paid_order()
        order.status = 'completed'
        order.save(update_fields=['status'])
        self.client.force_authenticate(self.admin)
        resp = self.client.post(
            f'/api/v1/admin/telegram-orders/{order.id}/confirm/', {}, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_confirm_processing_order_blocked_409(self):
        """Double-spend himoyasi: urinilgan (processing) buyurtma qayta tasdiqlanmaydi."""
        order = self._make_paid_order()
        order.status = 'processing'
        fv = dict(order.field_values or {})
        fv['_fragment_attempted'] = '1'
        order.field_values = fv
        order.save(update_fields=['status', 'field_values'])
        self.client.force_authenticate(self.admin)
        with mock.patch('apps.services.fragment_api.buy_premium') as bp:
            resp = self.client.post(
                f'/api/v1/admin/telegram-orders/{order.id}/confirm/', {}, format='json')
        self.assertEqual(resp.status_code, 409, resp.content)
        bp.assert_not_called()

    def test_confirm_unpaid_order_400(self):
        order = Order.objects.create(
            customer=self.customer, service=self.svc, package=self.premium_pkg,
            field_values={'username': '@durov'}, customer_name='C', customer_telegram='@c',
            total_price=self.premium_pkg.price, status='pending', payment_status='unpaid',
        )
        self.client.force_authenticate(self.admin)
        with mock.patch('apps.services.fragment_api.buy_premium') as bp:
            resp = self.client.post(
                f'/api/v1/admin/telegram-orders/{order.id}/confirm/', {}, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)
        bp.assert_not_called()

    def test_confirm_non_telegram_order_404(self):
        cat, _ = Category.objects.get_or_create(slug='mobile-games', defaults={'name': 'M'})
        other, _ = Service.objects.get_or_create(
            slug='pubg-mobile', defaults={'name': 'PUBG', 'category': cat})
        pkg, _ = Package.objects.get_or_create(service=other, name='60 UC', defaults={'price': 12000})
        order = Order.objects.create(customer=self.customer, service=other, package=pkg,
                                     field_values={'game_id': '123'}, customer_name='C',
                                     customer_telegram='@c', total_price=12000,
                                     payment_status='paid', status='pending')
        self.client.force_authenticate(self.admin)
        resp = self.client.post(
            f'/api/v1/admin/telegram-orders/{order.id}/confirm/', {}, format='json')
        self.assertEqual(resp.status_code, 404)

    # ── Rad qilish ──

    def test_reject_refunds_balance(self):
        order = self._make_paid_order()
        self.client.force_authenticate(self.admin)
        resp = self.client.post(
            f'/api/v1/admin/telegram-orders/{order.id}/reject/',
            {'cancel_reason': "Noto'g'ri username"}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()
        self.assertTrue(data['ok'])
        order.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.assertEqual(order.payment_status, 'refunded')
        self.assertEqual(order.cancel_reason, "Noto'g'ri username")
        # to'lov qaytarildi: 500000 + 125000 (refund)
        self.assertEqual(self.customer.balance, Decimal('625000'))
        self.assertEqual(data['refunded'], 125000.0)
        self.assertTrue(BalanceTransaction.objects.filter(
            user=self.customer, tx_type='refund', amount=order.total_price).exists())
        self.assertTrue(AuditLog.objects.filter(
            action='telegram_order_rejected', user=self.admin, target_id=order.id).exists())

    def test_reject_unpaid_no_refund(self):
        order = Order.objects.create(
            customer=self.customer, service=self.svc, package=self.premium_pkg,
            field_values={'username': '@durov'}, customer_name='C', customer_telegram='@c',
            total_price=self.premium_pkg.price, status='pending', payment_status='unpaid',
        )
        self.client.force_authenticate(self.admin)
        resp = self.client.post(
            f'/api/v1/admin/telegram-orders/{order.id}/reject/', {}, format='json')
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['refunded'], 0)
        order.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.assertEqual(order.payment_status, 'unpaid')
        self.assertFalse(BalanceTransaction.objects.filter(tx_type='refund').exists())

    def test_reject_completed_order_400(self):
        order = self._make_paid_order()
        order.status = 'completed'
        order.save(update_fields=['status'])
        self.client.force_authenticate(self.admin)
        resp = self.client.post(
            f'/api/v1/admin/telegram-orders/{order.id}/reject/', {}, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)
