"""
Xavfsizlik mustahkamlash testlari (security hardening).

  1. dev-kod (telegram_id siz) production'da 403 qaytaradi — account
     takeover yo'li yopilgan.
  2. fragment-login ScopedRateThrottle bilan himoyalangan.
  3. demo-login DEBUG=False da 404.
  4. IDOR: buyurtma faqat egasiga.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.orders.models import Order, OrderStatus
from apps.services.models import Category, Service, Package
from apps.users.models import TelegramLoginCode
from apps.settings_app.models import Setting

User = get_user_model()


class DevCodeBlockedInProductionTests(TestCase):
    """SECURITY: dev-rejim kodi (javobda qaytadigan) production'da 403."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='sec_cust', email='sec_cust@tg.user', telegram_username='sec_cust',
            telegram_id='90001',
        )

    @override_settings(DEBUG=False)
    def test_code_not_returned_in_response_production(self):
        """telegram_id siz so'rov production'da 403 — kod javobda YO'Q."""
        with mock.patch('apps.users.views._verify_username_real',
                        return_value=({'username': '@sec_cust'}, None)):
            r = self.client.post('/api/v1/auth/login-code/', {'username': 'sec_cust'})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn('code', r.data)
        # Hech qanday kod yaratilmadi
        self.assertFalse(TelegramLoginCode.objects.exists())

    @override_settings(DEBUG=True)
    def test_code_returned_only_in_debug(self):
        with mock.patch('apps.users.views._verify_username_real',
                        return_value=({'username': '@sec_cust'}, None)):
            r = self.client.post('/api/v1/auth/login-code/', {'username': 'sec_cust'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('code', r.data)  # faqat DEBUG'da

    @override_settings(DEBUG=False)
    def test_telegram_id_path_still_works_production(self):
        """Telegram ichidagi oqim (telegram_id bilan) production'da ishlaydi."""
        from apps.settings_app.models import Setting
        Setting.set_setting('telegram_bot_token', '123456:TEST-TOKEN')
        Setting.clear_cache()
        with mock.patch('apps.users.views._bot_chat_username', return_value='sec_cust'), \
             mock.patch('apps.users.code_utils.send_code_to_chat', return_value=True):
            r = self.client.post(
                '/api/v1/auth/login-code/',
                {'username': 'sec_cust', 'telegram_id': '90001'},
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['status'], 'sent')
        self.assertNotIn('code', r.data)


class FragmentLoginThrottleTests(TestCase):
    """SECURITY: fragment-login brute-force himoyasi (20/min)."""

    def setUp(self):
        self.client = APIClient()

    @override_settings(DEBUG=False)
    def test_throttle_applied(self):
        with mock.patch('apps.users.views._get_info_with_retry',
                        return_value=({'username': '@x', 'name': '', 'photo': '',
                                       'is_premium': False}, None)):
            # ScopedRateThrottle: 20/min — 21-chi so'rov 429 bo'lishi kerak.
            codes = []
            for _ in range(21):
                r = self.client.post('/api/v1/auth/fragment-login/', {'username': 'x'})
                codes.append(r.status_code)
        self.assertIn(429, codes, f'429 kutilgan edi, olindi: {codes}')


class DemoLoginBlockedTests(TestCase):
    """SECURITY: demo-login production'da 404."""

    @override_settings(DEBUG=False)
    def test_demo_login_404_in_production(self):
        r = APIClient().post('/api/v1/auth/demo-login/', {'role': 'admin'})
        self.assertEqual(r.status_code, 404)

    @override_settings(DEBUG=True)
    def test_demo_login_works_in_debug(self):
        r = APIClient().post('/api/v1/auth/demo-login/', {'role': 'customer'})
        self.assertEqual(r.status_code, 200)


class FragmentLoginTakeoverProtectionTests(TestCase):
    """SECURITY: fragment-login account-takeover himoyasi.

    Mavjud user uchun getInfo xato bo'lsa fallback FAQAT bot.getChat
    orqali telegram_id<->username mosligi tasdiqlanganda o'tadi. Aks holda
    haker istalgan mavjud username bilan o'sha user sifatida kira olardi.
    """

    def setUp(self):
        self.client = APIClient()
        self.victim = User.objects.create_user(
            username='takeover_victim', email='tv@tg.user',
            telegram_username='takeover_victim', telegram_id='70001',
        )

    def test_hacker_without_telegram_id_cannot_login_as_existing_user(self):
        """getInfo xato + telegram_id yo'q -> login rad etiladi."""
        with mock.patch('apps.services.fragment_api.get_info',
                        return_value={'error': 'not_found'}):
            r = self.client.post('/api/v1/auth/fragment-login/', {'username': 'takeover_victim'})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.data.get('next_step'), 'code')

    def test_hacker_with_wrong_telegram_id_cannot_login(self):
        """getInfo xato + noto'g'ri telegram_id -> rad etiladi."""
        with mock.patch('apps.services.fragment_api.get_info',
                        return_value={'error': 'not_found'}), \
             mock.patch('apps.users.views._bot_chat_username', return_value='someone_else'):
            r = self.client.post('/api/v1/auth/fragment-login/',
                                 {'username': 'takeover_victim', 'telegram_id': '99999'})
        self.assertEqual(r.status_code, 401)

    def test_owner_with_matching_telegram_id_logs_in(self):
        """To'g'ri telegram_id + getChat mosligi -> login o'tadi."""
        with mock.patch('apps.services.fragment_api.get_info',
                        return_value={'error': 'not_found'}), \
             mock.patch('apps.users.views._bot_chat_username', return_value='takeover_victim'):
            r = self.client.post('/api/v1/auth/fragment-login/',
                                 {'username': 'takeover_victim', 'telegram_id': '70001'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('access', r.data)

    def test_verified_get_info_still_logs_in_without_telegram_id(self):
        """getInfo muvaffaqiyatli bo'lsa telegram_id shart emas (Fragment
        tasdiqlashning o'zi)."""
        with mock.patch('apps.services.fragment_api.get_info',
                        return_value={'username': '@takeover_victim', 'name': 'V',
                                      'photo': '', 'is_premium': False}):
            r = self.client.post('/api/v1/auth/fragment-login/', {'username': 'takeover_victim'})
        self.assertEqual(r.status_code, 200)


class OrderIDORProtectionTests(TestCase):
    """SECURITY: IDOR — buyurtma faqat egasi ko'ra oladi."""

    def setUp(self):
        self.cat, _ = Category.objects.get_or_create(slug='idor-cat', defaults={'name': 'I'})
        self.svc, _ = Service.objects.get_or_create(
            slug='idor-svc', defaults={'name': 'S', 'category': self.cat, 'is_active': True},
        )
        self.pkg, _ = Package.objects.get_or_create(
            service=self.svc, name='P',
            defaults={'amount_label': 'P', 'price': '10000'},
        )
        self.owner = User.objects.create_user(
            username='idor_owner', email='idor_owner@tg.user', telegram_id='80001',
        )
        self.other = User.objects.create_user(
            username='idor_other', email='idor_other@tg.user', telegram_id='80002',
        )
        self.order = Order.objects.create(
            customer=self.owner, service=self.svc, package=self.pkg,
            customer_name='x', customer_telegram='x',
            total_price='10000', status=OrderStatus.PENDING, payment_status='unpaid',
        )
        self.client = APIClient()

    def test_other_user_cannot_read_foreign_order(self):
        self.client.force_authenticate(self.other)
        r = self.client.get(f'/api/v1/orders/{self.order.id}/')
        self.assertEqual(r.status_code, 404)  # "No Order matches"

    def test_owner_reads_own_order(self):
        self.client.force_authenticate(self.owner)
        r = self.client.get(f'/api/v1/orders/{self.order.id}/')
        self.assertEqual(r.status_code, 200)

    def test_other_user_cannot_update_foreign_order(self):
        self.client.force_authenticate(self.other)
        r = self.client.patch(f'/api/v1/orders/{self.order.id}/', {'status': 'completed'})
        self.assertIn(r.status_code, (403, 404, 405))
