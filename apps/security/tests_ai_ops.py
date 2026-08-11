"""
AI OPS tests — system health, AI error analysis, auto-fix.

  • system_health — komponent holatini yig'ish (hech qachon yiqilmaydi)
  • ai_ops.analyze_error — Gemini tahlili (fail-safe)
  • ai_ops.report_error_to_staff — staff guruhiga xabar (throttled)
  • auto_fix.run_auto_fix — avto-tuzatish oqimi
"""
from unittest import mock

from django.test import TestCase, override_settings

from apps.settings_app.models import Setting
from apps.security import system_health
from apps.security import ai_ops
from apps.security import auto_fix


class SystemHealthTests(TestCase):
    def setUp(self):
        Setting.clear_cache()

    def test_collect_health_never_crashes(self):
        parts = system_health.collect_health()
        self.assertGreaterEqual(len(parts), 4)
        for p in parts:
            self.assertIn(p['status'], ('ok', 'down'))

    def test_health_summary_shape(self):
        summary = system_health.health_summary()
        self.assertIn('ok', summary)
        self.assertIn('down', summary)
        self.assertIn('components', summary)

    def test_format_health_report_html(self):
        report = system_health.format_health_report()
        self.assertIn('<b>', report)

    def test_recent_errors_empty_db(self):
        errors = system_health.recent_errors(5)
        self.assertIsInstance(errors, list)


class AiOpsAnalyzeTests(TestCase):
    def setUp(self):
        Setting.clear_cache()
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')

    def test_analyze_not_configured(self):
        Setting.set_setting('gemini_api_key', '')
        result = ai_ops.analyze_error({'kind': 'x', 'component': 'y'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'ai_not_configured')

    @mock.patch('urllib.request.urlopen')
    def test_analyze_valid_response(self, mock_urlopen):
        mock_urlopen.return_value.__enter__.return_value.read.return_value = (
            b'{"candidates":[{"content":{"parts":[{"text": "{\\"root_cause\\": \\"views.py: api call\\", '
            b'\\"severity\\": \\"HIGH\\", \\"fix_steps\\": [\\"check token\\"], '
            b'\\"auto_fixable\\": true, \\"admin_summary\\": \\"test\\"}"}]}}]}'
        )
        result = ai_ops.analyze_error({'kind': 'login', 'component': 'views.py',
                                       'error_code': 'X'})
        self.assertTrue(result['ok'])
        self.assertEqual(result['severity'], 'HIGH')
        self.assertTrue(result['auto_fixable'])
        self.assertEqual(result['fix_steps'], ['check token'])

    @mock.patch('urllib.request.urlopen', side_effect=Exception('network down'))
    def test_analyze_network_failure_fails_safe(self, _m):
        result = ai_ops.analyze_error({'kind': 'login', 'component': 'views.py'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'network_error')

    def test_scrub_token_in_detail(self):
        text = ai_ops._scrub('token 123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh in url')
        self.assertNotIn('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh', text)
        self.assertIn('[REDACTED]', text)


class AiOpsReportTests(TestCase):
    def setUp(self):
        Setting.clear_cache()
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        Setting.set_setting('telegram_bot_token', '123:ABC')
        Setting.set_setting('payment_report_chat_id', '-100123')

    AI_OK = {'ok': True, 'root_cause': 'views.py: fragment verify',
             'severity': 'HIGH', 'fix_steps': ['check token'],
             'auto_fixable': True, 'admin_summary': 'test'}

    @mock.patch('apps.security.ai_ops._send_to_group', return_value=True)
    @mock.patch('apps.security.ai_ops.analyze_error', return_value=AI_OK)
    def test_report_sends_to_group(self, mock_ai, mock_send):
        ok = ai_ops.report_error_to_staff(
            {'kind': 'login', 'component': 'views.py', 'error_code': 'X'},
            throttle_key='test_throttle',
        )
        self.assertTrue(ok)
        mock_send.assert_called_once()
        # Xabar matni HTML va komponent nomini o'z ichiga oladi
        text = mock_send.call_args[0][0]
        self.assertIn('views.py', text)
        self.assertIn('views.py: fragment verify', text)  # AI tahlil natijasi bor

    @mock.patch('apps.security.ai_ops._send_to_group', return_value=True)
    @mock.patch('apps.security.ai_ops.analyze_error', return_value=AI_OK)
    def test_report_throttled(self, mock_ai, mock_send):
        ok1 = ai_ops.report_error_to_staff(
            {'kind': 'login', 'component': 'views.py'},
            throttle_key='same_key', throttle_seconds=600,
        )
        ok2 = ai_ops.report_error_to_staff(
            {'kind': 'login', 'component': 'views.py'},
            throttle_key='same_key', throttle_seconds=600,
        )
        self.assertTrue(ok1)
        self.assertFalse(ok2)  # ikkinchisi throttle ichida
        self.assertEqual(mock_send.call_count, 1)

    @mock.patch('apps.security.ai_ops._send_to_group', return_value=True)
    def test_report_without_gemini_still_sends_fallback(self, mock_send):
        Setting.set_setting('gemini_api_key', '')
        ok = ai_ops.report_error_to_staff(
            {'kind': 'bot_polling', 'component': 'bot.py', 'error_code': 'conflict_409'},
            throttle_key='x',
        )
        self.assertTrue(ok)
        text = mock_send.call_args[0][0]
        self.assertIn('conflict_409', text)


class AutoFixTests(TestCase):
    def setUp(self):
        Setting.clear_cache()

    @mock.patch('apps.security.auto_fix._run_powershell', return_value=(0, 'ok'))
    @mock.patch('apps.security.auto_fix._is_port_open', return_value=True)
    @mock.patch('apps.security.system_health.health_summary',
                return_value={'ok': True, 'down': [],
                              'components': [
                                  {'name': 'Bot', 'status': 'ok', 'detail': ''},
                                  {'name': 'Backend', 'status': 'ok', 'detail': ''},
                                  {'name': 'Tunnel', 'status': 'ok', 'detail': ''},
                                  {'name': 'User Client', 'status': 'ok', 'detail': ''},
                              ]})
    def test_auto_fix_all_ok(self, _h, _p, _ps):
        result = auto_fix.run_auto_fix('tester')
        self.assertTrue(result['ok'])
        self.assertIn('summary', result)

    @mock.patch('apps.security.auto_fix._run_powershell', return_value=(0, 'ok'))
    @mock.patch('apps.security.auto_fix._is_port_open', return_value=False)
    @mock.patch('apps.security.auto_fix._taskkill_pids', return_value=1)
    @mock.patch('apps.security.system_health.health_summary',
                return_value={'ok': False, 'down': [{'name': 'Backend', 'detail': 'down'}],
                              'components': [
                                  {'name': 'Bot', 'status': 'ok', 'detail': ''},
                                  {'name': 'Backend', 'status': 'down', 'detail': ''},
                                  {'name': 'Tunnel', 'status': 'ok', 'detail': ''},
                                  {'name': 'User Client', 'status': 'ok', 'detail': ''},
                              ]})
    def test_auto_fix_restarts_backend(self, _h, _k, _p, _ps):
        result = auto_fix.run_auto_fix('tester')
        actions = result['actions']
        self.assertTrue(any('Backend' in a['component'] or 'Watchdog' in a['component']
                            for a in actions))

    def test_format_fix_report(self):
        result = {'ok': True, 'actions': [
            {'component': 'Backend', 'action': 'qayta ishga tushirildi', 'result': 'OK'},
        ], 'summary': 'Hammasi tiklandi'}
        report = auto_fix.format_fix_report(result)
        self.assertIn('Backend', report)
        self.assertIn('Hammasi tiklandi', report)
