# -*- coding: utf-8 -*-
"""Staff AI (staff guruhi yordamchisi) testlari — DONZO."""
import unittest

from django.test import TestCase

from apps.settings_app.models import Setting
from apps.security import staff_ai


class StaffAiTests(TestCase):
    def setUp(self):
        Setting.set_setting('gemini_api_key', '')
        Setting.set_setting('security_ai_enabled', 'False')
        Setting.set_setting('staff_ai_enabled', 'True')
        Setting.set_setting('staff_ai_throttle_ai_user', '')

    def test_is_enabled_requires_all_switches(self):
        self.assertFalse(staff_ai.is_enabled())
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        self.assertTrue(staff_ai.is_enabled())
        Setting.set_setting('staff_ai_enabled', 'false')
        self.assertFalse(staff_ai.is_enabled())

    def test_not_configured_message(self):
        result = staff_ai.staff_chat('holat qanday?', 'ai_user')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'ai_not_configured')
        self.assertIn('AI sozlanmagan', result['answer'])

    def test_throttle_limits_per_user(self):
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        with unittest.mock.patch.object(staff_ai, '_call_gemini',
                                        return_value={'ok': True, 'answer': 'ok'}):
            ok_count = 0
            for _ in range(staff_ai.THROTTLE_LIMIT + 3):
                r = staff_ai.staff_chat('savol', 'ai_user')
                if r.get('ok'):
                    ok_count += 1
        self.assertEqual(ok_count, staff_ai.THROTTLE_LIMIT)
        # Boshqa foydalanuvchi o'z limitiga ega
        with unittest.mock.patch.object(staff_ai, '_call_gemini',
                                        return_value={'ok': True, 'answer': 'ok'}):
            self.assertTrue(staff_ai.staff_chat('savol', 'boshqa_user')['ok'])

    def test_success_answer_and_escape(self):
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        with unittest.mock.patch.object(
                staff_ai, '_call_gemini',
                return_value={'ok': True, 'answer': 'Karta ***3064. Token: <b>12345</b>'}):
            r = staff_ai.staff_chat('karta qaysi?', 'ai_user')
        self.assertTrue(r['ok'])
        self.assertEqual(r['answer'], 'Karta ***3064. Token: <b>12345</b>')
        self.assertEqual(staff_ai.escape_html(r['answer']),
                         'Karta ***3064. Token: &lt;b&gt;12345&lt;/b&gt;')

    def test_live_context_never_raises(self):
        ctx = staff_ai._live_context()
        self.assertIn('TIZIM HOLATI', ctx)
        self.assertIn('STATISTIKA', ctx)

    def test_staff_chat_never_raises(self):
        # Gemini chaqiruvi xato bersa ham dict qaytadi, exception emas
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        with unittest.mock.patch.object(staff_ai, '_call_gemini',
                                        side_effect=Exception('boom')):
            r = staff_ai.staff_chat('savol', 'ai_user')
        self.assertIn('ok', r)
        self.assertFalse(r['ok'])
