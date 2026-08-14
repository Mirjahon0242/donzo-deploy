# -*- coding: utf-8 -*-
"""Staff AI (staff guruhi yordamchisi) testlari — DONZO."""
import json
import time
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

    def test_greeting_gets_instant_jarvis_answer(self):
        # Salomlashish Gemini'siz tezkor javob oladi (JARVIS uslubi)
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        with unittest.mock.patch.object(staff_ai, '_call_gemini',
                                        return_value={'ok': True, 'answer': 'GEMINI'}) as mock_call:
            r = staff_ai.staff_chat('Salom!', 'ai_user')
            self.assertTrue(r['ok'])
            self.assertTrue(r['answer'] and len(r['answer']) > 0)
            mock_call.assert_not_called()  # Gemini chaqirilmadi
        # Variantlar
        for g in ['Assalomu alaykum', 'hey', 'Qalaysiz?', 'Hi', 'Bormisiz']:
            with unittest.mock.patch.object(staff_ai, '_call_gemini',
                                            return_value={'ok': True, 'answer': 'GEMINI'}):
                r = staff_ai.staff_chat(g, 'ai_user')
                self.assertTrue(r['ok'], g)

    def test_greeting_includes_live_status_snippet(self):
        # JARVIS har javobida jonli status satri qo'shadi
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        with unittest.mock.patch.object(staff_ai, '_call_gemini',
                                        return_value={'ok': True, 'answer': 'GEMINI'}):
            r = staff_ai.staff_chat('Salom!', 'ai_user')
        self.assertTrue(r['ok'])
        # Status snippet belgisi javobda bor (hatto DB bo'sh bo'lsa ham fallback)
        self.assertIn('📊', r['answer'])

    def test_status_snippet_never_raises(self):
        # _status_snippet hech qachon exception tashlamaydi
        out = staff_ai._status_snippet()
        self.assertIsInstance(out, str)
        self.assertTrue(out.startswith('📊'))

    def test_greeting_does_not_consume_throttle(self):
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        # Limitni to'ldiramiz
        Setting.set_setting('staff_ai_throttle_ai_user',
                            json.dumps([time.time()] * staff_ai.THROTTLE_LIMIT))
        with unittest.mock.patch.object(staff_ai, '_call_gemini',
                                        return_value={'ok': True, 'answer': 'GEMINI'}) as mock_call:
            r = staff_ai.staff_chat('Salom!', 'ai_user')
            self.assertTrue(r['ok'])  # Throttle'ga qaramay javob beradi
            mock_call.assert_not_called()

    def test_non_greeting_hits_throttle(self):
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        Setting.set_setting('staff_ai_throttle_ai_user',
                            json.dumps([time.time()] * staff_ai.THROTTLE_LIMIT))
        with unittest.mock.patch.object(staff_ai, '_call_gemini',
                                        return_value={'ok': True, 'answer': 'GEMINI'}):
            r = staff_ai.staff_chat('karta qaysi?', 'ai_user')
            self.assertFalse(r['ok'])
            self.assertEqual(r['error'], 'throttled')

    # ── SUHBAT OQIMI (belgilangan tartib) testlari ────────────────────────

    def test_conv_advance_flow_order(self):
        # start → answer → detail → done → start (belgilangan tartib)
        self.assertEqual(staff_ai._conv_advance('start', 'karta holati qanday?'), 'answer')
        self.assertEqual(staff_ai._conv_advance('answer', 'batafsil ko\'rsat'), 'detail')
        self.assertEqual(staff_ai._conv_advance('detail', 'rahmat, yetarli'), 'done')
        self.assertEqual(staff_ai._conv_advance('done', 'yana savol'), 'start')

    def test_conv_advance_ending_words(self):
        # 'rahmat / tamom / yetarli' → done bosqichiga olib boradi
        for w in ['rahmat', 'tamom', 'yetarli', "bo'ldi", 'hammasi shu']:
            self.assertEqual(staff_ai._conv_advance('answer', w), 'done', w)
            self.assertEqual(staff_ai._conv_advance('detail', w), 'done', w)

    def test_conv_advance_detail_words(self):
        # 'batafsil / ko'rsat / davom' → detail bosqichiga o'tadi
        for w in ['batafsil ko\'rsat', 'davom et', 'qarangchi']:
            self.assertEqual(staff_ai._conv_advance('answer', w), 'detail', w)

    def test_conv_save_load_roundtrip(self):
        # Suhbat holati Setting'da saqlanadi va qayta o'qiladi
        data = {'step': 'detail', 'history': [{'role': 'user', 'text': 'salom'}], 'ts': time.time()}
        staff_ai._conv_save('flow_user', data)
        loaded = staff_ai._conv_load('flow_user')
        self.assertEqual(loaded['step'], 'detail')
        self.assertEqual(loaded['history'][0]['text'], 'salom')

    def test_conv_expires_after_ttl(self):
        # 10 daqiqadan ko'p harakatsizlik → yangi suhbat (start bosqichi)
        # (Setting'ga to'g'ridan-to'g'ri yozamiz — _conv_save ts'ni yangilaydi)
        old = {'step': 'detail', 'history': [], 'ts': time.time() - staff_ai.CONV_TTL_SECONDS - 5}
        Setting.set_setting(staff_ai.CONV_KEY_PREFIX + 'old_user', json.dumps(old))
        loaded = staff_ai._conv_load('old_user')
        self.assertEqual(loaded['step'], 'start')

    def test_staff_chat_advances_and_remembers(self):
        # To'liq oqim: start → answer → detail; tarix saqlanadi
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        Setting.set_setting('staff_ai_conv_flow2_user', '')
        with unittest.mock.patch.object(
                staff_ai, '_call_gemini',
                return_value={'ok': True, 'answer': 'OK'}):
            r1 = staff_ai.staff_chat('holat qanday?', 'flow2_user')
            self.assertTrue(r1['ok'])
        conv1 = staff_ai._conv_load('flow2_user')
        self.assertEqual(conv1['step'], 'answer')  # start → answer
        self.assertEqual(len(conv1['history']), 2)  # user + assistant
        with unittest.mock.patch.object(
                staff_ai, '_call_gemini',
                return_value={'ok': True, 'answer': 'Batafsil: OK'}):
            r2 = staff_ai.staff_chat('batafsil ko\'rsat', 'flow2_user')
            self.assertTrue(r2['ok'])
        conv2 = staff_ai._conv_load('flow2_user')
        self.assertEqual(conv2['step'], 'detail')  # answer → detail
        self.assertEqual(len(conv2['history']), 4)
        # Prompt tarixni o'z ichiga olgan (Gemini chaqiruvi prompt'ida)
        with unittest.mock.patch.object(
                staff_ai, '_call_gemini',
                return_value={'ok': True, 'answer': 'Xulosa'}):
            r3 = staff_ai.staff_chat('rahmat, yetarli', 'flow2_user')
            self.assertTrue(r3['ok'])
        conv3 = staff_ai._conv_load('flow2_user')
        self.assertEqual(conv3['step'], 'done')  # detail → done

    def test_staff_chat_history_limits(self):
        # Tarix cheksiz o'smaydi — CONV_HISTORY_MAX bilan cheklanadi
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        Setting.set_setting('staff_ai_conv_flow3_user', '')
        with unittest.mock.patch.object(
                staff_ai, '_call_gemini',
                return_value={'ok': True, 'answer': 'ok'}):
            for i in range(15):
                staff_ai.staff_chat(f'savol {i}', 'flow3_user')
        conv = staff_ai._conv_load('flow3_user')
        self.assertLessEqual(len(conv['history']), staff_ai.CONV_HISTORY_MAX * 2)
