# -*- coding: utf-8 -*-
"""
BOT ORQALI TASDIQLASH KODI tests.

Login amalga oshmasa foydalanuvchi username kiritadi → backend kod yaratib
@DONZOROBOT orqali shu foydalanuvchining Telegram chatiga yuboradi →
foydalanuvchi kodni web app'ga kiritadi → JWT.

  POST /api/v1/auth/login-code/           {username, telegram_id?}
  POST /api/v1/auth/login-code/verify/    {username, code}
"""
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.settings_app.models import Setting
from apps.users.models import TelegramLoginCode, User, Role
from apps.users.code_utils import hash_code


@override_settings(DEBUG=True)
class LoginCodeTests(TestCase):
    def setUp(self):
        cache.clear()
        Setting.clear_cache()
        Setting.set_setting('fragment_api_key', 'test-key')
        Setting.set_setting('telegram_bot_token', 'test-bot-token')
        self.client = APIClient()
        self.url = '/api/v1/auth/login-code/'
        self.verify_url = '/api/v1/auth/login-code/verify/'

    # ── Kod so'rash ──

    def test_missing_username_400(self):
        resp = self.client.post(self.url, {'telegram_id': '123'}, format='json')
        self.assertEqual(resp.status_code, 400)

    @mock.patch('apps.users.views._bot_chat_username', return_value='uz_ultra')
    def test_code_sent_via_bot_not_returned(self, _m):
        # Telegram ichida: getChat username'ni tasdiqlaydi (uz_ultra) → kod yuboriladi
        with mock.patch('apps.users.code_utils.send_code_to_chat', return_value=True) as send:
            resp = self.client.post(self.url, {'username': 'uz_ultra', 'telegram_id': '123456789'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'sent')
        self.assertNotIn('code', resp.data)  # kod javobda YO'Q
        # Kod bot orqali chatga yuborildi
        send.assert_called_once()
        args = send.call_args
        self.assertEqual(args.args[1], '123456789')
        self.assertRegex(args.args[2], r'^\d{6}$')
        # DB'da faqat hash saqlanadi
        rec = TelegramLoginCode.objects.filter(telegram_id='123456789', used=False).first()
        self.assertIsNotNone(rec)
        self.assertEqual(rec.code, hash_code(args.args[2]))
        self.assertNotEqual(rec.code, args.args[2])
        self.assertEqual(rec.telegram_username, 'uz_ultra')

    def test_bot_not_started_returns_400_with_guidance(self):
        # Bot foydalanuvchini topa olmasa (getChat muvaffaqiyatsiz, Start
        # bosilmagan) — 400 + yo'l ko'rsatma
        with mock.patch('apps.users.views._bot_chat_username', return_value=None):
            resp = self.client.post(self.url, {'username': 'uz_ultra', 'telegram_id': '123456789'}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Start', resp.data['detail'])

    def test_username_mismatch_403(self):
        # telegram_id ning username'i kiritilganga mos kelmasa — 403
        # (boshqa birovning username'ini egallashning oldi olinadi)
        with mock.patch('apps.users.views._bot_chat_username', return_value='other_user'):
            resp = self.client.post(self.url, {'username': 'uz_ultra', 'telegram_id': '123456789'}, format='json')
        self.assertEqual(resp.status_code, 403)
        self.assertIn('mos emas', resp.data['detail'])

    @mock.patch('apps.users.views._verify_username_real', return_value=({}, None))
    def test_dev_mode_returns_code_in_response(self, _m):
        # telegram_id yo'q (Telegramdan tashqari/dev) — kod javobda qaytadi
        resp = self.client.post(self.url, {'username': 'uz_ultra'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'dev')
        self.assertRegex(resp.data['code'], r'^\d{6}$')

    def test_fake_username_rejected_outside_telegram(self):
        # Telegramdan tashqarida (telegram_id yo'q): Username Fragment'da ham,
        # bazada ham yo'q — kod yuborilmaydi
        with mock.patch('apps.users.views._verify_username_real',
                        return_value=(None, 'FRAGMENT_ERROR')):
            resp = self.client.post(self.url, {'username': 'nobody_real'}, format='json')
        self.assertEqual(resp.status_code, 400)

    # ── Kodni tekshirish ──

    def _make_code(self, username='uz_ultra', telegram_id='111222', plain='123456', **kw):
        return TelegramLoginCode.objects.create(
            code=hash_code(plain),
            telegram_id=telegram_id,
            telegram_username=username,
            first_name='Test',
            last_name='User',
            language_code='uz',
            expires_at=kw.get('expires_at', timezone.now() + timedelta(minutes=5)),
        )

    def test_missing_fields_400(self):
        resp = self.client.post(self.verify_url, {}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_wrong_code_400(self):
        self._make_code()
        resp = self.client.post(self.verify_url, {'username': 'uz_ultra', 'code': '999999'}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_expired_code_400(self):
        self._make_code(expires_at=timezone.now() - timedelta(minutes=1))
        resp = self.client.post(self.verify_url, {'username': 'uz_ultra', 'code': '123456'}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_success_returns_jwt(self):
        self._make_code()
        resp = self.client.post(self.verify_url, {'username': 'uz_ultra', 'code': '123456'}, format='json')
        self.assertEqual(resp.status_code, 200)
        data = resp.data
        self.assertIn('access', data)
        self.assertIn('refresh', data)
        self.assertEqual(data['user']['username'], 'uz_ultra')
        # Foydalanuvchi yaratildi / topildi
        user = User.objects.get(username='uz_ultra')
        self.assertEqual(user.telegram_username, 'uz_ultra')
        # Telegram akkaunt id bog'landi
        self.assertEqual(user.telegram_id, '111222')

    def test_code_single_use(self):
        self._make_code()
        r1 = self.client.post(self.verify_url, {'username': 'uz_ultra', 'code': '123456'}, format='json')
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.post(self.verify_url, {'username': 'uz_ultra', 'code': '123456'}, format='json')
        self.assertEqual(r2.status_code, 400)

    def test_code_bound_to_username(self):
        # Kod boshqa username bilan ishlatib bo'lmaydi
        self._make_code(username='uz_ultra')
        resp = self.client.post(self.verify_url, {'username': 'someone_else', 'code': '123456'}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_existing_user_role_kept(self):
        User.objects.create_user(
            username='mirsaid0707', email='mirsaid0707@fragment.user',
            telegram_username='mirsaid0707', role='admin',
        )
        self._make_code(username='mirsaid0707', telegram_id='333444')
        resp = self.client.post(self.verify_url, {'username': 'mirsaid0707', 'code': '123456'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['user']['role'], 'admin')
        self.assertEqual(resp.data['user']['id'],
                         User.objects.get(username='mirsaid0707').pk)

    def test_admin_username_gets_super_admin(self):
        Setting.set_setting('fragment_admin_usernames', 'mirjahon_qochqorov,owner')
        self._make_code(username='mirjahon_qochqorov', telegram_id='555666')
        resp = self.client.post(self.verify_url, {'username': 'mirjahon_qochqorov', 'code': '123456'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['user']['role'], 'super_admin')

    def test_token_works_for_profile(self):
        self._make_code(username='premchi')
        token = self.client.post(self.verify_url, {'username': 'premchi', 'code': '123456'}, format='json').data['access']
        r = self.client.get('/api/v1/auth/profile/', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['username'], 'premchi')
