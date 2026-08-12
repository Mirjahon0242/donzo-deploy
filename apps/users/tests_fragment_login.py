"""
FRAGMENT LOGIN tests.

Web app ochilganda foydalanuvchi Telegram username kiritadi → backend
Fragment API (getInfo) orqali jonli ma'lumot oladi → shu ma'lumot login
sifatida qabul qilinadi, User yozuviga (user id) biriktiriladi va JWT
qaytariladi. Foydalanuvchi keyin user id orqali aniqlanadi.

  POST /api/v1/auth/fragment-login/  {username}
"""
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from apps.settings_app.models import Setting
from apps.users.models import User

FRAGMENT_INFO = {
    'username': '@test_user',
    'name': 'Test User',
    'photo': 'https://cdn.example/photo.jpg',
    'is_premium': True,
}


def _mock_get_info(username, timeout=30):
    uname = username.lstrip('@').lower()
    if uname == 'test_user':
        return dict(FRAGMENT_INFO, username='@test_user')
    if uname == 'mirjahon':
        return {'username': '@mirjahon', 'name': 'Mirjahon', 'photo': '', 'is_premium': False}
    return {'username': f'@{uname}', 'name': '', 'photo': '', 'is_premium': False,
            'error': {'code': 'FRAGMENT_ERROR', 'message': 'Topilmadi'}}


class FragmentLoginTests(TestCase):
    def setUp(self):
        Setting.clear_cache()
        Setting.set_setting('fragment_api_key', 'test-key')
        self.client = APIClient()

    @mock.patch('apps.users.views._get_info_with_retry', side_effect=lambda u: (_mock_get_info(u), None))
    def test_new_user_created_and_fragment_data_attached(self, _m):
        resp = self.client.post('/api/v1/auth/fragment-login/', {'username': 'test_user'}, format='json')
        self.assertEqual(resp.status_code, 200)
        data = resp.data
        self.assertIn('access', data)
        self.assertEqual(data['user']['username'], 'test_user')
        self.assertEqual(data['user']['first_name'], 'Test User')
        self.assertEqual(data['user']['avatar_url'], 'https://cdn.example/photo.jpg')
        self.assertTrue(data['user']['is_telegram_premium'])
        self.assertEqual(data['user']['role'], 'customer')
        # Data attached to the user id — retrievable by id
        user = User.objects.get(pk=data['user']['id'])
        self.assertEqual(user.telegram_username, 'test_user')
        self.assertEqual(user.first_name, 'Test User')
        self.assertTrue(user.is_telegram_premium)

    @mock.patch('apps.services.fragment_api.get_info', return_value=dict(FRAGMENT_INFO))
    def test_existing_user_enriched_and_role_kept(self, _m):
        existing = User.objects.create_user(
            username='test_user', email='test_user@fragment.user',
            telegram_username='test_user', role='operator',
        )
        resp = self.client.post('/api/v1/auth/fragment-login/', {'username': 'test_user'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['user']['id'], existing.pk)
        self.assertEqual(resp.data['user']['role'], 'operator')  # rol saqlanadi
        existing.refresh_from_db()
        self.assertEqual(existing.first_name, 'Test User')
        self.assertTrue(existing.is_telegram_premium)

    @mock.patch('apps.users.views._get_info_with_retry', side_effect=lambda u: (_mock_get_info(u), None))
    def test_admin_username_gets_super_admin(self, _m):
        Setting.set_setting('fragment_admin_usernames', 'mirjahon,owner')
        resp = self.client.post('/api/v1/auth/fragment-login/', {'username': 'Mirjahon'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['user']['role'], 'super_admin')

    @mock.patch('apps.users.views._get_info_with_retry',
                return_value=({'error': {'code': 'FRAGMENT_ERROR', 'message': 'Topilmadi'}}, 'FRAGMENT_ERROR'))
    def test_fragment_error_rejects_login(self, _m):
        resp = self.client.post('/api/v1/auth/fragment-login/', {'username': 'nobody'}, format='json')
        self.assertEqual(resp.status_code, 401)
        self.assertIn('FRAGMENT_ERROR', resp.data['detail'])

    @mock.patch('apps.services.fragment_api.get_info', return_value=dict(FRAGMENT_INFO))
    def test_telegram_username_must_match_typed_username(self, _m):
        """Qo'lda kiritilgan username JORIY Telegram akkaunt username'iga mos
        kelmasa login 403 — boshqa birovning username'ini kiritib bo'lmaydi."""
        resp = self.client.post('/api/v1/auth/fragment-login/',
                                {'username': 'someone_else', 'telegram_username': 'my_real_username'},
                                format='json')
        self.assertEqual(resp.status_code, 403)
        self.assertIn('mos emas', resp.data['detail'])

    @mock.patch('apps.services.fragment_api.get_info', return_value=dict(FRAGMENT_INFO))
    def test_telegram_username_matching_typed_allows_login(self, _m):
        """Kiritilgan username Telegram username'iga mos kelsa login o'tadi
        (katta/kichik harf farqi normalizatsiya qilinadi)."""
        resp = self.client.post('/api/v1/auth/fragment-login/',
                                {'username': 'Test_User', 'telegram_username': 'test_user'},
                                format='json')
        self.assertEqual(resp.status_code, 200)

    @mock.patch('apps.services.fragment_api.get_info', return_value=dict(FRAGMENT_INFO))
    def test_login_without_telegram_username_still_works(self, _m):
        """telegram_username yuborilmasa (Telegramdan tashqari/dev) tekshiruv
        o'tkazib yuboriladi — eski oqim buzilmaydi."""
        resp = self.client.post('/api/v1/auth/fragment-login/',
                                {'username': 'test_user'}, format='json')
        self.assertEqual(resp.status_code, 200)

    @mock.patch('apps.services.fragment_api.get_info',
                return_value={'username': '@uz_ultra', 'name': '', 'photo': '', 'is_premium': False,
                              'error': {'code': 'FRAGMENT_ERROR', 'message': 'Topilmadi'}})
    @mock.patch('apps.users.views._bot_chat_username', return_value='uz_ultra')
    def test_existing_user_falls_back_when_fragment_fails(self, _m, _m2):
        """Fragment API topa olmasa ham — telegram_id getChat bilan
        tasdiqlansa — bazadagi mavjud mijoz kira oladi (uz_ultra kabi real
        mijozlar bloklanmasligi uchun)."""
        existing = User.objects.create_user(
            username='uz_ultra', email='uz_ultra@fragment.user',
            telegram_username='uz_ultra', role='customer',
        )
        resp = self.client.post('/api/v1/auth/fragment-login/',
                                {'username': 'uz_ultra', 'telegram_id': '5709391089'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['user']['id'], existing.pk)
        self.assertEqual(resp.data['user']['username'], 'uz_ultra')
        # Rol saqlanadi, yangi Fragment ma'lumoti yo'q — eski ma'lumot buzilmaydi
        existing.refresh_from_db()
        self.assertEqual(existing.role, 'customer')

    @mock.patch('apps.services.fragment_api.get_info',
                return_value={'username': '@uz_ultra', 'name': '', 'photo': '', 'is_premium': False,
                              'error': {'code': 'FRAGMENT_ERROR', 'message': 'Topilmadi'}})
    @mock.patch('apps.users.views._bot_chat_username', return_value=None)
    def test_existing_user_without_telegram_id_rejected_on_fallback(self, _m, _m2):
        """SECURITY: Fragment xato bo'lsa, telegram_id/getChat tasdiqsiz
        mavjud user kirishi RAD etiladi (account takeover himoyasi)."""
        User.objects.create_user(
            username='uz_ultra', email='uz_ultra@fragment.user',
            telegram_username='uz_ultra', role='customer',
        )
        resp = self.client.post('/api/v1/auth/fragment-login/', {'username': 'uz_ultra'}, format='json')
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.data.get('next_step'), 'code')

    @mock.patch('apps.services.fragment_api.get_info',
                return_value={'username': '@mokhinur_logist', 'name': '', 'photo': '', 'is_premium': False,
                              'error': {'code': 'RATE_LIMIT', 'message': 'limit'}})
    @mock.patch('apps.users.views._bot_chat_username', return_value='mokhinur_logist')
    def test_existing_user_falls_back_on_rate_limit(self, _m, _m2):
        """Fragment API rate-limit berganida ham (getChat tasdiqlab) mavjud
        user kira oladi."""
        User.objects.create_user(
            username='mokhinur_logist', email='mokhinur_logist@fragment.user',
            telegram_username='mokhinur_logist', role='customer',
        )
        resp = self.client.post('/api/v1/auth/fragment-login/',
                                {'username': 'mokhinur_logist', 'telegram_id': '8802441950'}, format='json')
        self.assertEqual(resp.status_code, 200)

    def test_missing_username_400(self):
        resp = self.client.post('/api/v1/auth/fragment-login/', {}, format='json')
        self.assertEqual(resp.status_code, 400)

    @mock.patch('apps.users.views._get_info_with_retry', side_effect=lambda u: (_mock_get_info(u), None))
    def test_username_normalization(self, _m):
        resp = self.client.post('/api/v1/auth/fragment-login/', {'username': '  @Test_User  '}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['user']['username'], 'test_user')

    @mock.patch('apps.users.views._get_info_with_retry', side_effect=lambda u: (_mock_get_info(u), None))
    def test_token_works_for_orders(self, _m):
        resp = self.client.post('/api/v1/auth/fragment-login/', {'username': 'test_user'}, format='json')
        token = resp.data['access']
        r2 = self.client.get('/api/v1/auth/profile/', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.data['id'], resp.data['user']['id'])
