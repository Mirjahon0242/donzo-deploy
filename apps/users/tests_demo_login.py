"""
DEMO MODE login tests.

Login tizimi butunlay olib tashlangan; yagona kirish — demo-login:
  POST /api/v1/auth/demo-login/  {role: customer|admin|operator|support}
  → {access, refresh, user}

Har bir rol uchun bitta demo-foydalanuvchi get_or_create qilinadi
(idempotent), profil endpointlari JWT bilan ishlayveradi.
"""
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.users.models import User


# demo-login FAQAT DEBUG (lokal) uchun — production'da 404. Testlar bu
# endpoint'ni tekshirgani uchun DEBUG=True qilib ochiladi.
@override_settings(DEBUG=True)
class DemoLoginTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_customer_demo_login_returns_jwt_and_user(self):
        resp = self.client.post('/api/v1/auth/demo-login/', {'role': 'customer'}, format='json')
        self.assertEqual(resp.status_code, 200)
        data = resp.data
        self.assertIn('access', data)
        self.assertIn('refresh', data)
        self.assertEqual(data['user']['role'], 'customer')
        self.assertTrue(User.objects.filter(username='demo_customer').exists())

    def test_admin_demo_login_never_grants_staff(self):
        # XAVFSIZLIK: demo-login hech qachon super_admin bermaydi — faqat
        # customer. (Tunnel orqali {role:'admin'} yuborib har kim egasi
        # bo'lishi mumkin edi — endi yopiq.)
        resp = self.client.post('/api/v1/auth/demo-login/', {'role': 'admin'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['user']['role'], 'customer')
        self.assertNotEqual(resp.data['user']['role'], 'super_admin')

    def test_unknown_role_falls_back_to_customer(self):
        resp = self.client.post('/api/v1/auth/demo-login/', {'role': 'hacker'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['user']['role'], 'customer')

    @override_settings(DEBUG=False)
    def test_disabled_in_production(self):
        # Production (DEBUG=False): demo-login 404 — hech kim demo orqali
        # kirib bo'lmaydi.
        resp = self.client.post('/api/v1/auth/demo-login/', {'role': 'admin'}, format='json')
        self.assertEqual(resp.status_code, 404)

    def test_demo_login_is_idempotent(self):
        self.client.post('/api/v1/auth/demo-login/', {'role': 'customer'}, format='json')
        self.client.post('/api/v1/auth/demo-login/', {'role': 'customer'}, format='json')
        self.assertEqual(User.objects.filter(username='demo_customer').count(), 1)

    def test_old_telegram_endpoints_are_gone(self):
        # Login tizimi butunlay o'chirilgan — eski endpointlar 404 qaytaradi.
        resp = self.client.post(
            '/api/v1/auth/telegram/webapp/',
            {'init_data': 'whatever'}, format='json',
        )
        self.assertEqual(resp.status_code, 404)
        resp = self.client.post('/api/v1/auth/login/', {'email': 'x@y.z', 'password': 'x'}, format='json')
        self.assertEqual(resp.status_code, 404)

    def test_profile_works_with_demo_token(self):
        token = self.client.post('/api/v1/auth/demo-login/', {'role': 'customer'}, format='json').data['access']
        # Profile view fragment sync'ni background thread'da ishga tushirib,
        # test DB teardown'da lock qoldirishi mumkin — mock qilamiz.
        from unittest import mock
        with mock.patch('apps.users.views.sync_fragment_profile', return_value=None):
            resp = self.client.get('/api/v1/auth/profile/', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['username'], 'demo_customer')
