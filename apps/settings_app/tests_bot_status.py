"""
Bot status / bot_stats tests.

Covers the live bot-health data the admin 'Bot holati' panel shows:
  • token_status (getMe validation result)
  • last_heartbeat + heartbeat age
  • polling_errors (409 Conflict / NetworkError / timeout)
  • uptime_seconds
"""
import os
import tempfile
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

import bot_stats
from apps.users.models import User
from apps.settings_app.models import Setting

TEST_TOKEN = '1234567890:TESTtokentokentokentokentokenTEST'


class BotStatsHelpersTests(TestCase):
    """Pure bot_stats helpers — point STATS_FILE at a temp path."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig = bot_stats.STATS_FILE
        bot_stats.STATS_FILE = os.path.join(self.tmpdir, 'bot-stats.json')

    def tearDown(self):
        bot_stats.STATS_FILE = self._orig

    def test_heartbeat_sets_last_heartbeat(self):
        bot_stats.heartbeat()
        stats = bot_stats.read_bot_stats()
        self.assertIsNotNone(stats['last_heartbeat'])
        self.assertIsNotNone(stats['last_activity'])

    def test_record_polling_error_appends_and_caps(self):
        for i in range(25):
            bot_stats.record_polling_error('conflict_409', f'err {i}')
        stats = bot_stats.read_bot_stats()
        self.assertEqual(len(stats['polling_errors']), 20)  # capped at 20
        self.assertEqual(stats['polling_errors'][-1]['kind'], 'conflict_409')
        self.assertIn('err 24', stats['polling_errors'][-1]['message'])

    def test_set_token_status(self):
        bot_stats.set_token_status(True, username='TopTupUzbot', detail='@TopTupUzbot')
        stats = bot_stats.read_bot_stats()
        self.assertTrue(stats['token_status']['valid'])
        self.assertEqual(stats['token_status']['username'], 'TopTupUzbot')


class BotStatusViewTests(TestCase):
    """GET /api/v1/admin/bot-status/ — admin-only live bot health."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username='admin', email='admin@test.uz', password='x',
            role='super_admin', is_staff=True, is_superuser=True,
        )
        Setting.set_setting('telegram_bot_token', TEST_TOKEN)
        self.client.force_authenticate(user=self.admin)

    def test_bot_status_returns_token_and_polling_data(self):
        fake_stats = {
            'started_at': '2026-08-01T00:00:00+00:00',
            'last_activity': '2026-08-01T01:00:00+00:00',
            'last_heartbeat': '2026-08-01T01:00:00+00:00',
            'restarts': 2,
            'messages_sent': 5,
            'updates_handled': 7,
            'commands': {'start': 3},
            'token_status': {
                'checked_at': '2026-08-01T01:00:00+00:00',
                'valid': True,
                'username': 'TopTupUzbot',
                'detail': '@TopTupUzbot',
            },
            'polling_errors': [
                {'ts': '2026-08-01T01:00:00+00:00', 'kind': 'network_error', 'message': 'network down'},
            ],
        }
        with mock.patch('apps.settings_app.views.read_bot_stats', return_value=fake_stats):
            resp = self.client.get('/api/v1/admin/bot-status/')
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body['token_configured'])
        self.assertEqual(body['stats']['token_status']['valid'], True)
        self.assertEqual(body['stats']['polling_errors'][0]['kind'], 'network_error')
        self.assertEqual(body['stats']['last_heartbeat'], fake_stats['last_heartbeat'])
        self.assertIsNotNone(body['stats']['uptime_seconds'])

    def test_bot_status_requires_admin(self):
        customer = User.objects.create_user(
            username='cust', email='c@test.uz', password='x', role='customer',
        )
        client = APIClient()
        client.force_authenticate(user=customer)
        resp = client.get('/api/v1/admin/bot-status/')
        self.assertIn(resp.status_code, (403, 401))
