"""Tests for Fragment API getInfo profile enrichment (fragment_profile.py)."""

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.users.models import User, Role
from apps.users.fragment_profile import (
    _apply_info,
    _sync_in_thread,
    SYNC_INTERVAL,
)


def make_user(**kw):
    defaults = dict(
        username='tg_user_1',
        email='tg_user_1@telegram.user',
        telegram_id='111',
        telegram_username='someuser',
        first_name='Old Name',
        role=Role.CUSTOMER,
    )
    defaults.update(kw)
    return User.objects.create(**defaults)


class ApplyInfoTests(TestCase):
    def test_applies_name_photo_premium(self):
        u = make_user()
        changed = _apply_info(u, {
            'username': '@someuser',
            'name': 'New Full Name',
            'photo': 'https://nft.fragment.com/photo.jpg',
            'is_premium': True,
        })
        self.assertTrue(changed)
        self.assertEqual(u.first_name, 'New Full Name')
        self.assertEqual(u.avatar_url, 'https://nft.fragment.com/photo.jpg')
        self.assertTrue(u.is_telegram_premium)

    def test_no_change_when_identical(self):
        u = make_user(first_name='Same', avatar_url='x', is_telegram_premium=False)
        changed = _apply_info(u, {
            'name': 'Same', 'photo': 'x', 'is_premium': False,
        })
        self.assertFalse(changed)

    def test_empty_name_does_not_wipe(self):
        u = make_user(first_name='Keep Me')
        changed = _apply_info(u, {'name': '', 'photo': '', 'is_premium': False})
        self.assertFalse(changed)
        self.assertEqual(u.first_name, 'Keep Me')


class SyncInThreadTests(TestCase):
    def test_skip_within_interval(self):
        u = make_user(fragment_synced_at=timezone.now())
        with mock.patch('apps.services.fragment_api.get_info') as gi:
            _sync_in_thread(u.pk, force=False)
            gi.assert_not_called()

    def test_no_username_sets_synced_at(self):
        u = make_user(telegram_username='')
        _sync_in_thread(u.pk, force=False)
        u.refresh_from_db()
        self.assertIsNotNone(u.fragment_synced_at)

    def test_not_configured_does_nothing(self):
        u = make_user()
        with mock.patch('apps.services.fragment_api.configured', return_value=False), \
             mock.patch('apps.services.fragment_api.get_info') as gi:
            _sync_in_thread(u.pk, force=True)
            gi.assert_not_called()

    def test_error_from_api_keeps_profile_and_sets_synced_at(self):
        # FRAGMENT_ERROR = foydalanuvchi topilmadi (doimiy) — synced_at belgilanadi.
        u = make_user()
        with mock.patch('apps.services.fragment_api.configured', return_value=True), \
             mock.patch('apps.services.fragment_api.get_info',
                        return_value={'username': '@someuser', 'error': {'code': 'FRAGMENT_ERROR'}}):
            _sync_in_thread(u.pk, force=True)
        u.refresh_from_db()
        self.assertEqual(u.first_name, 'Old Name')
        self.assertIsNotNone(u.fragment_synced_at)

    def test_network_error_does_not_set_synced_at(self):
        # NETWORK_ERROR = vaqtinchalik — synced_at qo'yilmaydi, keyingi
        # login/profilda darhol qayta uriniladi (24h blokirovka yo'q).
        u = make_user()
        with mock.patch('apps.services.fragment_api.configured', return_value=True), \
             mock.patch('apps.services.fragment_api.get_info',
                        return_value={'username': '@someuser', 'error': {'code': 'NETWORK_ERROR'}}):
            _sync_in_thread(u.pk, force=True)
        u.refresh_from_db()
        self.assertEqual(u.first_name, 'Old Name')
        self.assertIsNone(u.fragment_synced_at)

    def test_success_updates_profile_and_synced_at(self):
        u = make_user()
        with mock.patch('apps.services.fragment_api.configured', return_value=True), \
             mock.patch('apps.services.fragment_api.get_info',
                        return_value={'username': '@someuser', 'name': 'Fresh Name',
                                      'photo': 'https://img', 'is_premium': True}):
            _sync_in_thread(u.pk, force=True)
        u.refresh_from_db()
        self.assertEqual(u.first_name, 'Fresh Name')
        self.assertEqual(u.avatar_url, 'https://img')
        self.assertTrue(u.is_telegram_premium)
        self.assertIsNotNone(u.fragment_synced_at)

    def test_interval_constant_is_24h(self):
        self.assertEqual(SYNC_INTERVAL, timedelta(hours=24))


class SyncLogTests(TestCase):
    """Har bir Fragment API natijasi admin Loglar (AuditLog) bo'limiga yoziladi."""

    def test_success_writes_audit_log(self):
        u = make_user()
        with mock.patch('apps.services.fragment_api.configured', return_value=True), \
             mock.patch('apps.services.fragment_api.get_info',
                        return_value={'username': '@someuser', 'name': 'Fresh Name',
                                      'photo': 'https://img', 'is_premium': False}):
            _sync_in_thread(u.pk, force=True)

        from apps.audit_log.models import AuditLog
        log = AuditLog.objects.filter(action='fragment_sync', target_id=u.pk).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.target_type, 'User')
        self.assertIn('yangilandi', log.description)
        self.assertIn('someuser', log.description)

    def test_not_verified_writes_neutral_log(self):
        # FRAGMENT_ERROR endi XATO emas — login kabi fallback: profil
        # saqlangan holda qoladi, log'ga neytral holat yoziladi.
        u = make_user()
        with mock.patch('apps.services.fragment_api.configured', return_value=True), \
             mock.patch('apps.services.fragment_api.get_info',
                        return_value={'username': '@someuser', 'error': {'code': 'FRAGMENT_ERROR'}}):
            _sync_in_thread(u.pk, force=True)

        from apps.audit_log.models import AuditLog
        log = AuditLog.objects.filter(action='fragment_sync', target_id=u.pk).first()
        self.assertIsNotNone(log)
        self.assertNotIn('xato', log.description)  # xato emas
        self.assertIn('saqlandi', log.description)
        self.assertIn('FRAGMENT_ERROR', log.description)

    def test_transient_error_writes_error_log(self):
        # NETWORK_ERROR — vaqtinchalik, hali ham xato sifatida log'lanadi.
        u = make_user()
        with mock.patch('apps.services.fragment_api.configured', return_value=True), \
             mock.patch('apps.services.fragment_api.get_info',
                        return_value={'username': '@someuser', 'error': {'code': 'NETWORK_ERROR'}}):
            _sync_in_thread(u.pk, force=True)

        from apps.audit_log.models import AuditLog
        log = AuditLog.objects.filter(action='fragment_sync', target_id=u.pk).first()
        self.assertIsNotNone(log)
        self.assertIn('xato', log.description)
        self.assertIn('NETWORK_ERROR', log.description)

    def test_no_api_call_does_not_log(self):
        # Username yo'q — getInfo chaqirilmaydi, log ham yozilmaydi.
        u = make_user(telegram_username='')
        _sync_in_thread(u.pk, force=False)

        from apps.audit_log.models import AuditLog
        self.assertFalse(AuditLog.objects.filter(action='fragment_sync', target_id=u.pk).exists())


class ProfileSyncFragmentAPITests(TestCase):
    """POST /auth/profile/sync-fragment/ — Web App ochilganda profilni
    Fragment bilan HOZIROQ to'ldiradi (force, sinxron, IDOR xavfsiz)."""

    def setUp(self):
        from rest_framework.test import APIClient
        # Throttle cache (locmem) test metodlari orasida saqlanadi — har
        # test oldidan tozalab, 'fragment_sync' scope'da 429 bo'lmasligini
        # ta'minlaymiz (6/min limit yetarli bo'lmay qolishi mumkin).
        from django.core.cache import cache
        cache.clear()
        self.user = make_user()
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = '/api/v1/auth/profile/sync-fragment/'

    def test_grace_window_skips_external_api(self):
        # 5 daqiqalik grace: yaqinda sync bo'lgan bo'lsa tashqi Fragment API
        # chaqirilmaydi — status 'fresh', profil joriy holatda qaytadi.
        self.user.fragment_synced_at = timezone.now()
        self.user.save(update_fields=['fragment_synced_at'])
        with mock.patch('apps.users.fragment_profile._sync_user') as sync:
            r = self.client.post(self.url, {}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['status'], 'fresh')
        sync.assert_not_called()

    def test_grace_expired_calls_api(self):
        # Grace muddati o'tgan (6 daqiqa oldin) — force-sync bajariladi.
        self.user.fragment_synced_at = timezone.now() - timedelta(minutes=6)
        self.user.save(update_fields=['fragment_synced_at'])
        with mock.patch('apps.users.fragment_profile._sync_user', return_value='updated') as sync:
            r = self.client.post(self.url, {}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['status'], 'updated')
        sync.assert_called_once()

    def test_authenticated_returns_fresh_user(self):
        with mock.patch('apps.users.fragment_profile._sync_user', return_value='updated') as sync:
            r = self.client.post(self.url, {}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['status'], 'updated')
        self.assertEqual(r.data['user']['id'], self.user.id)
        sync.assert_called_once()
        # force=True ishlatilgan — 24 soat interval chetlab o'tiladi
        self.assertTrue(sync.call_args.kwargs.get('force', False) or sync.call_args.args[1] is True)

    def test_unauthorized_401(self):
        from rest_framework.test import APIClient
        anon = APIClient()
        r = anon.post(self.url, {}, format='json')
        self.assertEqual(r.status_code, 401)

    def test_sync_error_still_returns_user(self):
        # Fragment API xatosi bo'lsa ham javob 200 va user qaytadi — profil
        # Telegram'dagi mavjud ma'lumotlar bilan ko'rsatiladi (buzilmaydi).
        with mock.patch('apps.users.fragment_profile._sync_user', side_effect=Exception('boom')):
            r = self.client.post(self.url, {}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['status'], 'error')
        self.assertEqual(r.data['user']['id'], self.user.id)

    def test_other_user_cannot_be_synced(self):
        # IDOR himoyasi: endpoint faqat request.user'ni sinxronlaydi — boshqa
        # user ID'sini yuborish hech narsa o'zgartirmaydi.
        make_user(username='other', email='other@tg.user', telegram_id='999',
                  telegram_username='otheruser')
        # return_value muhim: response'dagi 'status' maydoni JSON'da
        # serializatsiya qilinadi — MagicMock (return_value'siz) cheksiz
        # rekursiyaga kirib test'ni osib qo'yadi.
        with mock.patch('apps.users.fragment_profile._sync_user', return_value='updated') as sync:
            r = self.client.post(self.url, {'user_id': 999}, format='json')
        self.assertEqual(r.status_code, 200)
        # request.user sinxronlandi, boshqa user emas
        self.assertEqual(sync.call_args.args[0].pk, self.user.pk)


class _FakeThread:
    """Thread'ni sinxron ishlatadi — test'da SQLite lock'ga urilmaslik uchun
    (TestCase transaction'ini haqiqiy thread'ga bo'lish mumkin emas)."""

    def __init__(self, target=None, args=(), kwargs=None, **kw):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)


class BulkSyncTests(TestCase):
    def test_sync_all_counts_and_status(self):
        from apps.users.fragment_profile import sync_all_fragment_profiles, get_bulk_sync_status

        u1 = make_user(username='tg_bulk_1', email='tg_bulk_1@tg.user', telegram_id='201')
        u2 = make_user(username='tg_bulk_2', email='tg_bulk_2@tg.user', telegram_id='202')
        make_user(username='tg_no_username', email='tg_no_username@tg.user', telegram_id='203', telegram_username='')

        with mock.patch('apps.users.fragment_profile.threading.Thread', _FakeThread), \
             mock.patch('apps.services.fragment_api.configured', return_value=True), \
             mock.patch('apps.services.fragment_api.get_info',
                        return_value={'username': '@u', 'name': 'Bulk Name',
                                      'photo': 'https://img', 'is_premium': True}):
            total = sync_all_fragment_profiles()

        st = get_bulk_sync_status()
        self.assertEqual(total, 2)  # faqat telegram_username'li lar
        self.assertEqual(st['total'], 2)
        self.assertEqual(st['updated'], 2)
        self.assertEqual(st['failed'], 0)
        self.assertFalse(st['running'])
        self.assertIsNotNone(st['finished_at'])

        u1.refresh_from_db()
        u2.refresh_from_db()
        self.assertEqual(u1.first_name, 'Bulk Name')
        self.assertTrue(u2.is_telegram_premium)
