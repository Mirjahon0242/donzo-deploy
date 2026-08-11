"""
ANTI-FRAUD metadata tests.

Foydalanuvchi haqida barcha ma'lumotlar (IP, joylashuv, qurilma, platforma,
vaqt mintaqasi, GPS) yig'ilib admin panelda ko'rinishi kerak:

  POST /api/v1/auth/profile/device-info/  — qurilma/joylashuv metadata
  AdminUserSerializer                     — admin API'da anti-fraud maydonlari
  _capture_login_meta                     — login paytida IP yig'ish
"""
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from apps.settings_app.models import Setting
from apps.users.models import User
from apps.users.serializers import AdminUserSerializer


class DeviceInfoTests(TestCase):
    def setUp(self):
        Setting.clear_cache()
        Setting.set_setting('fragment_api_key', 'test-key')
        self.user = User.objects.create_user(
            username='fraud_test', email='fraud@test.local',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_device_info_saves_metadata(self):
        resp = self.client.post('/api/v1/auth/profile/device-info/', {
            'platform': 'android',
            'language': 'uz',
            'timezone': 'Asia/Tashkent',
            'user_agent': 'Mozilla/5.0 (Linux; Android 14)',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.last_platform, 'android')
        self.assertEqual(self.user.last_language, 'uz')
        self.assertEqual(self.user.last_timezone, 'Asia/Tashkent')
        self.assertIn('Android', self.user.last_user_agent)
        self.assertIsNotNone(self.user.last_seen_at)

    def test_device_info_geo_coordinates(self):
        """Qurilmadan aniq GPS — geo_source='gps' belgilanadi (IP emas)."""
        resp = self.client.post('/api/v1/auth/profile/device-info/', {
            'lat': '41.311081', 'lng': '69.240562',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(float(self.user.geo_lat), 41.311081)
        self.assertEqual(float(self.user.geo_lng), 69.240562)
        self.assertEqual(self.user.geo_source, 'gps')

    @mock.patch('apps.users.geoip.reverse_geocode',
                return_value="Chilonzor tumani, Toshkent shahri, Toshkent, O'zbekiston")
    def test_device_info_gps_writes_full_address(self, mock_rev):
        """GPS kelganda reverse-geocoding orqali TO'LIQ manzil last_location'ga yoziladi."""
        resp = self.client.post('/api/v1/auth/profile/device-info/', {
            'lat': '41.311081', 'lng': '69.240562',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        mock_rev.assert_called_once()
        self.user.refresh_from_db()
        self.assertEqual(float(self.user.geo_lat), 41.311081)
        self.assertIn('Chilonzor', self.user.last_location or '')
        self.assertIn('Toshkent', self.user.last_location or '')

    @mock.patch('apps.users.geoip.reverse_geocode', return_value='')
    def test_device_info_gps_reverse_fail_keeps_old_location(self, mock_rev):
        """Reverse-geocode xato bo'lsa eski joylashuv buzilmaydi, koordinata saqlanadi."""
        self.user.last_location = "Toshkent, UZ"
        self.user.save()
        resp = self.client.post('/api/v1/auth/profile/device-info/', {
            'lat': '41.311081', 'lng': '69.240562',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.last_location, "Toshkent, UZ")
        self.assertEqual(float(self.user.geo_lat), 41.311081)

    def test_device_info_requires_auth(self):
        anon = APIClient()
        resp = anon.post('/api/v1/auth/profile/device-info/', {'platform': 'x'}, format='json')
        self.assertIn(resp.status_code, (401, 403))

    @mock.patch('apps.users.geoip.geolocate', return_value={
        'city': 'Toshkent', 'country': 'UZ', 'region': 'Toshkent', 'isp': 'UZMOBILE',
    })
    def test_device_info_ip_location(self, _m):
        resp = self.client.post(
            '/api/v1/auth/profile/device-info/', {},
            format='json',
            REMOTE_ADDR='213.230.93.180',
        )
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.last_ip, '213.230.93.180')
        self.assertIn('Toshkent', self.user.last_ip_location or '')
        self.assertIn('UZ', self.user.last_ip_location or '')
        # GPS to'liq manzili bo'lmagani uchun IP label umumiy joylashuvga ham yoziladi
        self.assertIn('Toshkent', self.user.last_location or '')

    @mock.patch('apps.users.geoip.geolocate', return_value={
        'city': 'Toshkent', 'country': 'UZ', 'region': 'Toshkent', 'isp': 'UZMOBILE',
        'lat': 41.311081, 'lon': 69.240562,
    })
    def test_device_info_ip_approximate_coordinates(self, _m):
        """GPS ruxsat bermagan bo'lsa ham IP bo'yicha taxminiy koordinata xaritada ko'rinadi."""
        resp = self.client.post(
            '/api/v1/auth/profile/device-info/', {},
            format='json',
            REMOTE_ADDR='213.230.93.180',
        )
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertAlmostEqual(float(self.user.geo_lat), 41.311081)
        self.assertAlmostEqual(float(self.user.geo_lng), 69.240562)
        # IP taxminiy koordinata — geo_source='ip' (GPS emas!)
        self.assertEqual(self.user.geo_source, 'ip')

    def test_device_info_gps_overrides_ip_coordinates(self):
        """GPS ruxsat berilsa — IP taxminiy emas, aniq GPS koordinata saqlanadi."""
        self.user.geo_lat = 41.31
        self.user.geo_lng = 69.24
        self.user.geo_source = 'ip'
        self.user.save()
        resp = self.client.post('/api/v1/auth/profile/device-info/', {
            'lat': '41.311081', 'lng': '69.240562',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(float(self.user.geo_lat), 41.311081)
        self.assertEqual(float(self.user.geo_lng), 69.240562)
        # IP taxminiy ustidan aniq GPS yozildi
        self.assertEqual(self.user.geo_source, 'gps')


class LoginMetaTests(TestCase):
    def setUp(self):
        Setting.clear_cache()
        Setting.set_setting('fragment_api_key', 'test-key')
        self.user = User.objects.create_user(
            username='meta_user', email='meta@test.local',
        )
        self.client = APIClient()

    @mock.patch('apps.users.views._get_info_with_retry',
                side_effect=lambda u: ({'username': '@meta_user', 'name': '', 'photo': '', 'is_premium': False}, None))
    def test_login_captures_ip(self, _m):
        resp = self.client.post(
            '/api/v1/auth/fragment-login/', {'username': 'meta_user'},
            format='json', REMOTE_ADDR='8.8.8.8',
        )
        self.assertEqual(resp.status_code, 200)
        user = User.objects.get(pk=resp.data['user']['id'])
        self.assertEqual(user.last_ip, '8.8.8.8')
        self.assertIsNotNone(user.last_seen_at)


class AdminSerializerFraudFieldsTests(TestCase):
    def setUp(self):
        Setting.clear_cache()
        self.user = User.objects.create_user(
            username='fraud_admin', email='fraud_admin@test.local',
            last_ip='91.204.10.1',
            last_ip_location='Toshkent, UZ · UZMOBILE',
            last_location="Chilonzor tumani, Toshkent shahri, Toshkent, O'zbekiston",
            last_platform='ios',
            last_language='uz',
            last_timezone='Asia/Tashkent',
            last_user_agent='Mozilla/5.0 (iPhone)',
        )

    def test_admin_serializer_includes_fraud_fields(self):
        data = AdminUserSerializer(self.user).data
        self.assertEqual(data['last_ip'], '91.204.10.1')
        self.assertIn('Chilonzor', data['last_location'])
        self.assertIn('UZMOBILE', data['last_ip_location'])
        self.assertEqual(data['last_platform'], 'ios')
        self.assertEqual(data['last_language'], 'uz')
        self.assertEqual(data['last_timezone'], 'Asia/Tashkent')
        self.assertIn('iPhone', data['last_user_agent'])
        self.assertIn('geo_lat', data)
        self.assertIn('geo_lng', data)
        self.assertIn('geo_source', data)
        self.assertIn('last_seen_at', data)
