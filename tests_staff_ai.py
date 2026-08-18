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

    def test_greeting_goes_through_gemini_dynamic(self):
        # Salomlashish ham Gemini orqali DINAMIK javob oladi — tayyor matn yo'q.
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        with unittest.mock.patch.object(staff_ai, '_call_gemini',
                                        return_value={'ok': True, 'answer': 'GEMINI'}) as mock_call:
            r = staff_ai.staff_chat('Salom!', 'ai_user')
            self.assertTrue(r['ok'])
            self.assertEqual(r['answer'], 'GEMINI')
            mock_call.assert_called_once()  # Gemini chaqirildi → javob dinamik
        # Variantlar ham Gemini orqali
        for g in ['Assalomu alaykum', 'hey', 'Qalaysiz?', 'Hi', 'Bormisiz']:
            with unittest.mock.patch.object(staff_ai, '_call_gemini',
                                            return_value={'ok': True, 'answer': 'GEMINI'}):
                r = staff_ai.staff_chat(g, 'ai_user')
                self.assertTrue(r['ok'], g)
                self.assertEqual(r['answer'], 'GEMINI', g)

    def test_greeting_uses_short_prompt_fast_path(self):
        # Greeting QISQA maxsus prompt bilan yuboriladi — to'liq kontekst yo'q,
        # lekin dinamik (Gemini har safar yozilganiga qarab javob tuzadi).
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        captured = {}

        def fake_call(prompt):
            captured['prompt'] = prompt
            return {'ok': True, 'answer': 'GEMINI'}

        with unittest.mock.patch.object(staff_ai, '_call_gemini', side_effect=fake_call):
            r = staff_ai.staff_chat('Salom!', 'ai_user')
            self.assertTrue(r['ok'])
        p = captured['prompt']
        # Qisqa greeting persona ishlatiladi
        self.assertIn('QISQA PERSONA', p)
        # To'liq og'ir kontekst YO'Q (tez javob uchun)
        self.assertNotIn('LIVE SYSTEM CONTEXT', p)
        self.assertNotIn('KATALOG', p)
        # Ser murojaati bor; hisobot majburiy emas (faqat so'ralganda)
        self.assertIn('ser', p)
        self.assertNotIn('STATUS SNIPPET', p)

    def test_greeting_uses_ser_addressing_and_no_fixed_text(self):
        # Persona'da doimiy matn yo'q — javob Gemini'ga yuborilgan savolga mos
        # tuziladi. Bu yerda prompt'da 'ser' murojaati borligini tekshiramiz.
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        captured = {}

        def fake_call(prompt):
            captured['prompt'] = prompt
            return {'ok': True, 'answer': 'GEMINI'}

        with unittest.mock.patch.object(staff_ai, '_call_gemini', side_effect=fake_call):
            r = staff_ai.staff_chat('Salom!', 'ai_user')
            self.assertTrue(r['ok'])
        # Persona 'ser' murojaatini o'z ichiga oladi va tayyor greeting ro'yxati yo'q
        self.assertIn('ser', captured['prompt'])
        self.assertNotIn('_GREETING_ANSWER', captured['prompt'])

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

    # ── REJIM (muloyim / angry) testlari ──────────────────────────────────

    def test_default_mode_is_gentle(self):
        # Default rejim — muloyim
        Setting.set_setting(staff_ai.ANGY_MODE_KEY, '')
        self.assertEqual(staff_ai._get_ai_mode(), 'gentle')

    def test_mode_on_command_switches_to_angry(self):
        # "donzo angry rejimini yoq" → agressiv rejimga o'tadi
        Setting.set_setting(staff_ai.ANGY_MODE_KEY, 'false')
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        r = staff_ai.staff_chat('donzo angry rejimini yoq', 'mode_user')
        self.assertTrue(r['ok'])
        self.assertIn('Angry', r['answer'])
        self.assertEqual(staff_ai._get_ai_mode(), 'angry')

    def test_mode_off_command_switches_to_gentle(self):
        # "angry rejimini o'chir" → muloyim rejimga qaytadi
        Setting.set_setting(staff_ai.ANGY_MODE_KEY, 'true')
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        r = staff_ai.staff_chat("angry rejimini o'chir", 'mode_user2')
        self.assertTrue(r['ok'])
        self.assertEqual(staff_ai._get_ai_mode(), 'gentle')

    def test_gentle_mode_repeat_gets_kind_reminder(self):
        # Muloyim rejimda takroriy savol — yumshoq eslatma, xijolat gapi EMAS
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        Setting.set_setting(staff_ai.ANGY_MODE_KEY, 'false')
        Setting.set_setting('staff_ai_conv_mode_rep_user', '')
        fake = unittest.mock.patch.object(staff_ai, '_call_gemini',
                                          return_value={'ok': True, 'answer': 'GEMINI'})
        fake.start()
        try:
            staff_ai.staff_chat('karta limiti qanday?', 'mode_rep_user')
            r2 = staff_ai.staff_chat('karta limiti qanday?', 'mode_rep_user')
            self.assertTrue(r2['ok'])
            self.assertNotEqual(r2['answer'], 'GEMINI')
            self.assertNotIn('egangnikini', r2['answer'].lower())
            self.assertIn('savol', r2['answer'].lower())
        finally:
            fake.stop()

    def test_angry_mode_repeat_gets_shame_line(self):
        # Angry rejimda takroriy savol — xijolat gapi
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        Setting.set_setting(staff_ai.ANGY_MODE_KEY, 'true')
        Setting.set_setting('staff_ai_conv_mode_rep2_user', '')
        fake = unittest.mock.patch.object(staff_ai, '_call_gemini',
                                          return_value={'ok': True, 'answer': 'GEMINI'})
        fake.start()
        try:
            staff_ai.staff_chat('karta limiti qanday?', 'mode_rep2_user')
            r2 = staff_ai.staff_chat('karta limiti qanday?', 'mode_rep2_user')
            self.assertTrue(r2['ok'])
            self.assertNotEqual(r2['answer'], 'GEMINI')
        finally:
            fake.stop()

    def test_repeat_question_gets_shame_line(self):
        # Xuddi shu savol takrorlansa — boshidan javob emas, xijolat gapi keladi
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        Setting.set_setting('staff_ai_conv_rep_user', '')
        fake = unittest.mock.patch.object(staff_ai, '_call_gemini',
                                          return_value={'ok': True, 'answer': 'GEMINI'})
        fake.start()
        try:
            r1 = staff_ai.staff_chat('karta limiti qanday ishlaydi?', 'rep_user')
            self.assertTrue(r1['ok'])
            self.assertEqual(r1['answer'], 'GEMINI')
            # Xuddi shu savol qayta so'raladi
            r2 = staff_ai.staff_chat('karta limiti qanday ishlaydi?', 'rep_user')
            self.assertTrue(r2['ok'])
            self.assertNotEqual(r2['answer'], 'GEMINI')
            self.assertGreater(len(r2['answer']), 10)
        finally:
            fake.stop()

    def test_repeat_does_not_trigger_on_first_question(self):
        # Birinchi marta so'ralgan savol — oddiy javob oladi
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        Setting.set_setting('staff_ai_conv_rep2_user', '')
        fake = unittest.mock.patch.object(staff_ai, '_call_gemini',
                                          return_value={'ok': True, 'answer': 'GEMINI'})
        fake.start()
        try:
            r = staff_ai.staff_chat('karta qaysi?', 'rep2_user')
            self.assertTrue(r['ok'])
            self.assertEqual(r['answer'], 'GEMINI')
        finally:
            fake.stop()

    def test_repeat_owner_always_gets_real_answer(self):
        # Ega (ser) takrorlasa ham — to'liq javob oladi, xijolat gapi YO'Q
        from apps.users.models import User
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        Setting.set_setting('super_admin_telegram_id', '999')
        Setting.set_setting('staff_ai_conv_rep3_user', '')
        u = User.objects.create_user(username='rep3_user', email='rep3@t.uz',
                                     password='x12345678', role='super_admin',
                                     telegram_id='999')
        fake = unittest.mock.patch.object(staff_ai, '_call_gemini',
                                          return_value={'ok': True, 'answer': 'GEMINI'})
        fake.start()
        try:
            staff_ai.staff_chat('karta limiti?', 'rep3_user')
            r2 = staff_ai.staff_chat('karta limiti?', 'rep3_user')
            self.assertEqual(r2['answer'], 'GEMINI')
        finally:
            fake.stop()

    def test_owner_prompt_contains_owner_rules(self):
        # Ega (ser) prompt'ida SHARTSIZ HURMAT + haqoratga o'z aybini bo'yniga
        # olish + to'liq tadqiqot qoidalari bor — har ikkala rejimda ham.
        from apps.users.models import User
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        Setting.set_setting('super_admin_telegram_id', '999')
        Setting.set_setting('staff_ai_conv_owner1_user', '')
        Setting.set_setting('staff_ai_memory_owner1_user', '')
        User.objects.create_user(username='owner1_user', email='owner1@t.uz',
                                 password='x12345678', role='super_admin',
                                 telegram_id='999')
        captured = {}

        def fake_call(prompt):
            captured['prompt'] = prompt
            return {'ok': True, 'answer': 'GEMINI'}

        # Angry rejimda ham, gentle rejimda ham blok bor
        for mode_cmd, check in (('donzo angry rejimini yoq', 'angry'),
                                ('angry rejimini o\'chir', 'gentle')):
            with unittest.mock.patch.object(staff_ai, '_call_gemini', side_effect=fake_call):
                staff_ai.staff_chat(mode_cmd, 'owner1_user')
                r = staff_ai.staff_chat('karta qaysi?', 'owner1_user')
                self.assertTrue(r['ok'], check)
            p = captured['prompt']
            self.assertIn('EGASI (SER) UCHUN MAXSUS QOIDALAR', p, check)
            self.assertIn('SHARTSIZ HURMAT', p, check)
            self.assertIn('o\'z aybini bo\'yniga olish', p, check)
            self.assertIn('onging past', p, check)
            self.assertIn('MA\'LUMOT TO\'PLASH', p, check)

    def test_owner_insult_still_goes_to_gemini_not_shame(self):
        # Ega "onging past" desa ham — xijolat gapi YO'Q, Gemini'ga to'liq
        # javob uchun boradi (takroriy-savol xijolati faqat boshqalarga).
        from apps.users.models import User
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        Setting.set_setting('super_admin_telegram_id', '999')
        Setting.set_setting('staff_ai_conv_owner2_user', '')
        User.objects.create_user(username='owner2_user', email='owner2@t.uz',
                                 password='x12345678', role='super_admin',
                                 telegram_id='999')
        fake = unittest.mock.patch.object(staff_ai, '_call_gemini',
                                          return_value={'ok': True, 'answer': 'Ha, ser, shundayman.'})
        fake.start()
        try:
            r1 = staff_ai.staff_chat('onging past ekan', 'owner2_user')
            r2 = staff_ai.staff_chat('onging past ekan', 'owner2_user')
            self.assertEqual(r1['answer'], 'Ha, ser, shundayman.')
            self.assertEqual(r2['answer'], 'Ha, ser, shundayman.')
        finally:
            fake.stop()

    def test_non_owner_prompt_has_no_owner_rules(self):
        # Oddiy staff prompt'ida egaga xos qoidalar bloki YO'Q
        from apps.users.models import User
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        Setting.set_setting('super_admin_telegram_id', '999')
        Setting.set_setting('staff_ai_conv_staff1_user', '')
        User.objects.create_user(username='staff1_user', email='staff1@t.uz',
                                 password='x12345678', role='admin',
                                 telegram_id='555')
        captured = {}

        def fake_call(prompt):
            captured['prompt'] = prompt
            return {'ok': True, 'answer': 'GEMINI'}

        with unittest.mock.patch.object(staff_ai, '_call_gemini', side_effect=fake_call):
            r = staff_ai.staff_chat('karta qaysi?', 'staff1_user')
            self.assertTrue(r['ok'])
        self.assertNotIn('EGASI (SER) UCHUN MAXSUS QOIDALAR', captured['prompt'])
        self.assertNotIn('SHARTSIZ HURMAT', captured['prompt'])

    def test_strict_mode_command_switches_mode(self):
        # "donzo qattiq rejimini yoq" → strict; "qattiq rejimini o'chir" → gentle
        Setting.set_setting(staff_ai.ANGY_MODE_KEY, 'false')
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        r = staff_ai.staff_chat('donzo qattiq rejimini yoq', 'mode_user')
        self.assertTrue(r['ok'])
        self.assertIn('Qattiq rejim', r['answer'])
        self.assertEqual(staff_ai._get_ai_mode(), 'strict')
        r2 = staff_ai.staff_chat("qattiq rejimini o'chir", 'mode_user')
        self.assertTrue(r2['ok'])
        self.assertEqual(staff_ai._get_ai_mode(), 'gentle')

    def test_strict_persona_in_prompt(self):
        # Strict rejimda prompt'da qattiq persona (sovuqqon, buyruqboz,
        # vazifa→maqsad→natija) bor; gentle'da yo'q
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        Setting.set_setting(staff_ai.ANGY_MODE_KEY, 'strict')
        Setting.set_setting('staff_ai_conv_strict1_user', '')
        captured = {}

        def fake_call(prompt):
            captured['prompt'] = prompt
            return {'ok': True, 'answer': 'GEMINI'}

        with unittest.mock.patch.object(staff_ai, '_call_gemini', side_effect=fake_call):
            r = staff_ai.staff_chat('karta qaysi?', 'strict1_user')
            self.assertTrue(r['ok'])
        p = captured['prompt']
        self.assertIn('qattiq rejim', p)
        self.assertIn('SOVUQQON, QAT\'IY, BUYRUBOZ', p)
        self.assertIn('VAZIFA', p)
        self.assertIn('MAQSAD', p)
        self.assertIn('NATIJA', p)
        self.assertIn('Intizom va tartib', p)

    def test_strict_owner_prompt_has_owner_rules_too(self):
        # Ega + strict rejim: qattiq persona BILAN birga egaga hurmat qoidalari ham bor
        from apps.users.models import User
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        Setting.set_setting('super_admin_telegram_id', '999')
        Setting.set_setting(staff_ai.ANGY_MODE_KEY, 'strict')
        Setting.set_setting('staff_ai_conv_owner3_user', '')
        User.objects.create_user(username='owner3_user', email='owner3@t.uz',
                                 password='x12345678', role='super_admin',
                                 telegram_id='999')
        captured = {}

        def fake_call(prompt):
            captured['prompt'] = prompt
            return {'ok': True, 'answer': 'GEMINI'}

        with unittest.mock.patch.object(staff_ai, '_call_gemini', side_effect=fake_call):
            r = staff_ai.staff_chat('karta qaysi?', 'owner3_user')
            self.assertTrue(r['ok'])
        p = captured['prompt']
        self.assertIn('SOVUQQON, QAT\'IY, BUYRUBOZ', p)
        self.assertIn('EGASI (SER) UCHUN MAXSUS QOIDALAR', p)
        self.assertIn('SHARTSIZ HURMAT', p)
        self.assertIn('COLD, STRICT and COMMANDING but always', p)

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
            for i in range(25):
                staff_ai.staff_chat(f'savol {i}', 'flow3_user')
        conv = staff_ai._conv_load('flow3_user')
        self.assertLessEqual(len(conv['history']), staff_ai.CONV_HISTORY_MAX * 2)

    def test_daily_context_included_in_prompt(self):
        # AI prompt'iga kunlik kontekst (bugun nima bo'ldi) qo'shiladi
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        captured = {}

        def fake_call(prompt):
            captured['prompt'] = prompt
            return {'ok': True, 'answer': 'OK'}

        with unittest.mock.patch.object(staff_ai, '_call_gemini', side_effect=fake_call):
            r = staff_ai.staff_chat('holat qanday?', 'flow4_user')
            self.assertTrue(r['ok'])
        p = captured.get('prompt', '')
        self.assertIn('== TODAY', p)
        self.assertIn('Yangi foydalanuvchilar', p)
        # Tarix limiti 30 ga oshirildi (xotira kengaytirildi)
        self.assertEqual(staff_ai.CONV_HISTORY_MAX, 30)

    def test_daily_context_never_raises(self):
        # _daily_context hech qachon exception tashlamaydi
        out = staff_ai._daily_context()
        self.assertIsInstance(out, str)
        self.assertTrue(len(out) > 0)

    def test_thinking_and_humanity_rules_in_prompt(self):
        # Prompt'da fikrlash (tahlil) va odamiylik qoidalari bor
        staff_ai._set_ai_mode('gentle')
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        captured = {}

        def fake_call(prompt):
            captured['prompt'] = prompt
            return {'ok': True, 'answer': 'OK'}

        with unittest.mock.patch.object(staff_ai, '_call_gemini', side_effect=fake_call):
            r = staff_ai.staff_chat('nimadir so\'rasam', 'flow5_user')
            self.assertTrue(r['ok'])
        p = captured.get('prompt', '')
        self.assertIn('fikrlash', p)
        self.assertIn('ODIAMIYLIK', p)
        self.assertIn('tahlil', p.lower())

    # ── MAXSUS STSENARIYLAR ────────────────────────────────────────────────
    def _mk_user(self, username, role):
        from apps.users.models import User
        return User.objects.create_user(username=username, email=f'{username}@t.uz',
                                        password='x12345678', role=role)

    # ── FOYDALANUVCHIGA SHAXSIY (LICHKA) XABAR ────────────────────────────

    def test_send_user_message_owner_sends(self):
        # "user ga habar yoz: matn" → foydalanuvchi lichkasiga xabar yuboriladi
        from apps.users.models import User
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        self._mk_user('dm_owner', 'super_admin')
        cust = self._mk_user('dm_customer', 'customer')
        cust.telegram_id = '998991234567'
        cust.save()
        with unittest.mock.patch.object(staff_ai, '_dm_user', return_value=True) as dm:
            r = staff_ai.staff_chat('dm_customer ga habar yoz: salom, buyurtmangiz tayyor', 'dm_owner')
        self.assertTrue(r['ok'])
        self.assertIn('yuborildi', r['answer'])
        dm.assert_called_once()
        sent_to, sent_text = dm.call_args[0]
        self.assertEqual(sent_to.id, cust.id)
        self.assertIn('buyurtmangiz tayyor', sent_text)
        # Audit log yozilgan
        from apps.audit_log.models import AuditLog
        self.assertTrue(AuditLog.objects.filter(action='ai_dm_sent', target_id=cust.id).exists())

    def test_send_user_message_find_by_telegram_username(self):
        # @telegram_username orqali ham topiladi
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        self._mk_user('dm_owner2', 'super_admin')
        cust = self._mk_user('dm_cust2', 'customer')
        cust.telegram_username = 'ultra_user'
        cust.telegram_id = '998111222333'
        cust.save()
        with unittest.mock.patch.object(staff_ai, '_dm_user', return_value=True):
            r = staff_ai.staff_chat('@ultra_user ga habar yubor: salom dost', 'dm_owner2')
        self.assertTrue(r['ok'])
        self.assertIn('yuborildi', r['answer'])

    def test_send_user_message_user_not_found(self):
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        self._mk_user('dm_owner3', 'super_admin')
        r = staff_ai.staff_chat('nobody123 ga habar yoz: salom', 'dm_owner3')
        self.assertFalse(r['ok'])
        self.assertIn('topilmadi', r['answer'])

    def test_send_user_message_no_telegram_id(self):
        # Telegram'ga bog'lanmagan foydalanuvchiga xabar yuborib bo'lmaydi
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        self._mk_user('dm_owner4', 'super_admin')
        self._mk_user('dm_notele', 'customer')
        with unittest.mock.patch.object(staff_ai, '_dm_user', return_value=True) as dm:
            r = staff_ai.staff_chat('dm_notele ga habar yoz: salom', 'dm_owner4')
        self.assertFalse(r['ok'])
        self.assertIn("bog'lanmagan", r['answer'])
        dm.assert_not_called()

    def test_send_user_message_denied_for_operator(self):
        # Operator bu buyruqni ishlata olmaydi — oddiy AI oqimiga tushadi
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        self._mk_user('dm_operator', 'operator')
        with unittest.mock.patch.object(staff_ai, '_dm_user', return_value=True) as dm:
            with unittest.mock.patch.object(staff_ai, '_call_gemini',
                                            return_value={'ok': True, 'answer': 'oddiy javob'}):
                r = staff_ai.staff_chat('dm_customer ga habar yoz: salom', 'dm_operator')
        self.assertTrue(r['ok'])
        self.assertEqual(r['answer'], 'oddiy javob')  # DM yuborilmadi
        dm.assert_not_called()

    def test_dm_user_plain_text_no_html_parse(self):
        # AI matni < > belgilar bilan ham buzilmasin — parse_mode YO'Q
        from apps.users.models import User
        Setting.set_setting('telegram_bot_token', '12345:TESTTOKEN')
        cust = self._mk_user('dm_cust_plain', 'customer')
        cust.telegram_id = '998777666555'
        cust.save()
        captured = {}

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(req, timeout=0):
            captured['body'] = json.loads(req.data.decode('utf-8'))
            return _FakeResp()

        with unittest.mock.patch.object(staff_ai.urllib.request, 'urlopen', side_effect=fake_urlopen):
            ok = staff_ai._dm_user(cust, 'Salom <b>dost</b> & do''st!')
        self.assertTrue(ok)
        self.assertEqual(captured['body']['chat_id'], '998777666555')
        self.assertEqual(captured['body']['text'], 'Salom <b>dost</b> & do''st!')
        self.assertNotIn('parse_mode', captured['body'])  # HTML parse yo'q

    def test_scenario_new_card_requires_admin_role(self):
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        self._mk_user('s_guest', 'guest')
        r = staff_ai.staff_chat('yangi karta qo\'shmoqchiman', 's_guest')
        self.assertFalse(r['ok'])
        self.assertIn('ruxsat yo\'q', r['answer'])

    def test_scenario_new_card_full_flow(self):
        from apps.cardpay.models import PaymentCard
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        self._mk_user('s_admin', 'admin')
        # 1) Stsenariy boshlanadi
        r = staff_ai.staff_chat('yangi karta qo\'shmoqchiman', 's_admin')
        self.assertTrue(r['ok'])
        self.assertIn('Karta raqamini yuboring', r['answer'])
        # 2) Raqam
        r = staff_ai.staff_chat('8600123412345678', 's_admin')
        self.assertIn('Karta egasi', r['answer'])
        # 3) Egas
        r = staff_ai.staff_chat('JAVLONBEK AKRAMOV', 's_admin')
        self.assertIn('Qaysi bank', r['answer'])
        # 4) Bank
        r = staff_ai.staff_chat('XALQ BANKI', 's_admin')
        self.assertIn('Limit', r['answer'])
        # 5) Limit
        r = staff_ai.staff_chat('5000000, 30', 's_admin')
        self.assertIn('tasdiqlaysizmi', r['answer'])
        # 6) Tasdiqlash → amal bajariladi
        r = staff_ai.staff_chat('ha', 's_admin')
        self.assertTrue(r['ok'])
        self.assertIn('qo\'shildi', r['answer'])
        self.assertTrue(PaymentCard.objects.filter(card_number='8600123412345678').exists())
        card = PaymentCard.objects.get(card_number='8600123412345678')
        self.assertEqual(card.card_holder, 'JAVLONBEK AKRAMOV')
        self.assertEqual(float(card.max_amount), 5000000)
        self.assertEqual(card.max_transfers, 30)
        # Stsenariy tugadi — holat tozalandi
        conv = staff_ai._conv_load('s_admin')
        self.assertEqual(conv.get('step'), 'start')
        self.assertNotIn('scenario', conv)

    def test_scenario_new_card_cancel(self):
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        self._mk_user('s_admin2', 'admin')
        r = staff_ai.staff_chat('yangi karta qo\'shish', 's_admin2')
        self.assertTrue(r['ok'])
        r = staff_ai.staff_chat('8600123412349999', 's_admin2')
        self.assertTrue(r['ok'])
        r = staff_ai.staff_chat('bekor qil', 's_admin2')
        self.assertIn('Bekor qilindi', r['answer'])
        from apps.cardpay.models import PaymentCard
        self.assertFalse(PaymentCard.objects.filter(card_number='8600123412349999').exists())

    def test_scenario_accept_payment_flow(self):
        from apps.cardpay.models import SuspiciousPayment
        from apps.users.models import User
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        user = self._mk_user('pay_customer', 'customer')
        admin = self._mk_user('pay_admin', 'admin')
        sp = SuspiciousPayment.objects.create(
            user=user, amount='100000', status='pending',
            note='test',
        )
        r = staff_ai.staff_chat('shubhali to\'lovni tasdiqlash kerak', 'pay_admin')
        self.assertTrue(r['ok'])
        r = staff_ai.staff_chat(str(sp.pk), 'pay_admin')
        self.assertIn('tasdiqlaysizmi', r['answer'])
        r = staff_ai.staff_chat('ha', 'pay_admin')
        self.assertTrue(r['ok'])
        self.assertTrue(r['answer'])
        sp.refresh_from_db()
        self.assertEqual(sp.status, 'approved')

    def test_scenario_complete_order_flow(self):
        from apps.orders.models import Order
        from apps.services.models import Service, Package, Category
        from apps.users.models import User
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        operator = self._mk_user('ord_operator', 'operator')
        customer = self._mk_user('ord_customer', 'customer')
        cat = Category.objects.create(name='Test', slug='test')
        svc = Service.objects.create(name='Xizmat', slug='xizmat', category=cat)
        pkg = Package.objects.create(service=svc, name='Paket', amount_label='100', price='10000')
        order = Order.objects.create(
            order_number='ORD-777', customer=customer, service=svc, package=pkg,
            field_values={}, customer_name='Test', customer_telegram='@t',
            total_price='10000', status='pending',
        )
        r = staff_ai.staff_chat('buyurtmani bajarish kerak', 'ord_operator')
        self.assertTrue(r['ok'])
        r = staff_ai.staff_chat('ORD-777', 'ord_operator')
        self.assertIn('bajarildi', r['answer'])
        r = staff_ai.staff_chat('ha', 'ord_operator')
        self.assertTrue(r['ok'])
        order.refresh_from_db()
        self.assertEqual(order.status, 'completed')

    def test_scenario_complete_order_requires_role(self):
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        self._mk_user('ord_guest', 'guest')
        r = staff_ai.staff_chat('buyurtma bajar', 'ord_guest')
        self.assertFalse(r['ok'])
        self.assertIn('ruxsat yo\'q', r['answer'])

    def test_scenario_change_price_flow(self):
        from apps.services.models import Service, Package, Category
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        self._mk_user('px_admin', 'admin')
        cat = Category.objects.create(name='O\'yinlar', slug='oyinlar')
        svc = Service.objects.create(name='PUBG UC', slug='pubg-uc', category=cat)
        pkg = Package.objects.create(service=svc, name='660 UC', amount_label='660', price='80000')
        r = staff_ai.staff_chat('narxni o\'zgartirish kerak', 'px_admin')
        self.assertTrue(r['ok'])
        r = staff_ai.staff_chat('1', 'px_admin')
        self.assertIn('660 UC', r['answer'])
        r = staff_ai.staff_chat('95000', 'px_admin')
        self.assertIn('tasdiqlaysizmi', r['answer'])
        r = staff_ai.staff_chat('ha', 'px_admin')
        self.assertTrue(r['ok'])
        pkg.refresh_from_db()
        self.assertEqual(float(pkg.price), 95000.0)

    def test_scenario_add_package_flow(self):
        from apps.services.models import Service, Category, Package
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        self._mk_user('ap_admin', 'admin')
        cat = Category.objects.create(name='O\'yinlar', slug='oyinlar2')
        svc = Service.objects.create(name='Free Fire', slug='free-fire', category=cat)
        r = staff_ai.staff_chat('yangi paket qo\'shish kerak', 'ap_admin')
        self.assertTrue(r['ok'])
        r = staff_ai.staff_chat('1', 'ap_admin')
        self.assertIn('Xizmat', r['answer'])
        r = staff_ai.staff_chat('1000 Donat', 'ap_admin')
        r = staff_ai.staff_chat('45000', 'ap_admin')
        self.assertIn('tasdiqlaysizmi', r['answer'])
        r = staff_ai.staff_chat('ha', 'ap_admin')
        self.assertTrue(r['ok'])
        pkg = Package.objects.filter(service=svc, name='1000 Donat').first()
        self.assertIsNotNone(pkg)
        self.assertEqual(float(pkg.price), 45000.0)

    def test_scenario_topup_balance_flow(self):
        from apps.users.models import User
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        self._mk_user('tb_admin', 'admin')
        self._mk_user('tb_customer', 'customer')
        r = staff_ai.staff_chat('balans to\'ldirish kerak', 'tb_admin')
        self.assertTrue(r['ok'])
        r = staff_ai.staff_chat('tb_customer', 'tb_admin')
        r = staff_ai.staff_chat('100000', 'tb_admin')
        self.assertIn('tasdiqlaysizmi', r['answer'])
        r = staff_ai.staff_chat('ha', 'tb_admin')
        self.assertTrue(r['ok'])
        u = User.objects.get(username='tb_customer')
        self.assertEqual(float(u.balance), 100000.0)

    def test_scenario_toggle_service_flow(self):
        from apps.services.models import Service, Package, Category
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        self._mk_user('tg_admin', 'admin')
        cat = Category.objects.create(name='Xizmatlar', slug='xizmatlar')
        svc = Service.objects.create(name='Netflix', slug='netflix', category=cat)
        pkg = Package.objects.create(service=svc, name='1 oy', amount_label='1oy', price='25000')
        r = staff_ai.staff_chat('xizmatni o\'chirish kerak', 'tg_admin')
        self.assertTrue(r['ok'])
        r = staff_ai.staff_chat('1', 'tg_admin')
        self.assertIn('Netflix', r['answer'])
        r = staff_ai.staff_chat('ha', 'tg_admin')
        self.assertTrue(r['ok'])
        svc.refresh_from_db()
        self.assertFalse(svc.is_active)

    def test_live_context_includes_catalog(self):
        from apps.services.models import Service, Package, Category
        cat = Category.objects.create(name='O\'yinlar', slug='oyinlar3')
        svc = Service.objects.create(name='PUBG UC', slug='pubg-uc-2', category=cat)
        Package.objects.create(service=svc, name='660 UC', amount_label='660', price='80000')
        ctx = staff_ai._live_context()
        self.assertIn('KATALOG', ctx)
        self.assertIn('PUBG UC', ctx)
        self.assertIn('660 UC', ctx)
        self.assertIn('80,000', ctx)

    def test_immediate_topup_no_confirm(self):
        # "darhol qil" — tasdiqlash savolisiz darhol bajariladi (super_admin)
        from apps.users.models import User
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        self._mk_user('im_owner', 'super_admin')
        self._mk_user('im_customer', 'customer')
        r = staff_ai.staff_chat('darhol balans to\'ldirish im_customer 75000', 'im_owner')
        self.assertTrue(r['ok'])
        u = User.objects.get(username='im_customer')
        self.assertEqual(float(u.balance), 75000.0)

    def test_immediate_change_price_no_confirm(self):
        # "darhol narx" — tasdiqlashsiz narx o'zgaradi
        from apps.services.models import Service, Package, Category
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        self._mk_user('im_admin2', 'super_admin')
        cat = Category.objects.create(name='O\'yinlar', slug='oyinlar-im')
        svc = Service.objects.create(name='PUBG', slug='pubg-im', category=cat)
        pkg = Package.objects.create(service=svc, name='660 UC', amount_label='660', price='80000')
        r = staff_ai.staff_chat('darhol narxni o\'zgartirish 1 95000', 'im_admin2')
        self.assertTrue(r['ok'])
        pkg.refresh_from_db()
        self.assertEqual(float(pkg.price), 95000.0)

    def test_immediate_complete_order_no_confirm(self):
        # "darhol buyurtma bajar" — tasdiqlashsiz bajariladi
        from apps.orders.models import Order
        from apps.services.models import Service, Package, Category
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        self._mk_user('im_oper2', 'super_admin')
        customer = self._mk_user('im_cust2', 'customer')
        cat = Category.objects.create(name='O\'yinlar', slug='oyinlar-im2')
        svc = Service.objects.create(name='FF', slug='ff-im', category=cat)
        pkg = Package.objects.create(service=svc, name='1000 Donat', amount_label='1000', price='45000')
        order = Order.objects.create(
            order_number='ORD-888', customer=customer, service=svc, package=pkg,
            field_values={}, customer_name='Test', customer_telegram='@t',
            total_price='45000', status='pending',
        )
        r = staff_ai.staff_chat('darhol buyurtmani bajarish ORD-888', 'im_oper2')
        self.assertTrue(r['ok'])
        order.refresh_from_db()
        self.assertEqual(order.status, 'completed')

    def test_immediate_denied_for_non_owner(self):
        # Egasi bo'lmagan (admin) uchun immediate rejim ishlamaydi — oddiy stsenariy
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        self._mk_user('im_admin3', 'admin')
        self._mk_user('im_cust3', 'customer')
        r = staff_ai.staff_chat('darhol balans to\'ldirish im_cust3 1000', 'im_admin3')
        # Admin uchun immediate ruxsat emas — stsenariy savoli keladi
        self.assertTrue(r['ok'])
        self.assertNotIn('✅', r['answer'])
        from apps.users.models import User
        u = User.objects.get(username='im_cust3')
        self.assertEqual(float(u.balance), 0.0)

    # ── UZOQ MUDDATLI XOTIRA (sessiyalar orasida eslab qolish) ────────────

    def test_memory_save_load_roundtrip(self):
        staff_ai._memory_save('mem_user', ['fakt 1', 'fakt 2'])
        self.assertEqual(staff_ai._memory_load('mem_user'), ['fakt 1', 'fakt 2'])

    def test_memory_update_records_question_and_preference(self):
        # Har muloqotdan so'ng xotiraga "so'radi" eslatmasi yoziladi,
        # xohish so'zlari (kinoya) alohida eslatma bo'ladi.
        staff_ai._memory_update('mem_user', 'qandaysan, menga kinoya bilan javob ber')
        notes = staff_ai._memory_load('mem_user')
        self.assertTrue(any('so\'radi' in n for n in notes))
        self.assertTrue(any('kinoya' in n for n in notes))

    def test_memory_persists_across_sessions(self):
        # Suhbat tarixi 10 daqiqada o'chadi, lekin UZOQ MUDDATLI xotira qoladi.
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        Setting.set_setting('staff_ai_conv_mem2_user', '')
        Setting.set_setting(staff_ai.MEMORY_KEY_PREFIX + 'mem2_user', '')
        with unittest.mock.patch.object(
                staff_ai, '_call_gemini',
                return_value={'ok': True, 'answer': 'OK'}):
            staff_ai.staff_chat('men paket narxlarini qanchaligini so\'rayman, qisqa javob ber', 'mem2_user')
        # Suhbat o'chiriladi (TTL) — xotira esa qoladi
        Setting.set_setting(staff_ai.CONV_KEY_PREFIX + 'mem2_user', '')
        Setting.set_setting('staff_ai_conv_mem2_user', '')
        notes = staff_ai._memory_load('mem2_user')
        self.assertTrue(len(notes) >= 1)
        self.assertTrue(any('paket' in n or 'so\'ra' in n for n in notes))
        # Yangi sessiyadagi prompt xotirani o'z ichiga oladi
        captured = {}

        def fake_call(prompt):
            captured['prompt'] = prompt
            return {'ok': True, 'answer': 'OK'}

        with unittest.mock.patch.object(staff_ai, '_call_gemini', side_effect=fake_call):
            staff_ai.staff_chat('yana savolim bor', 'mem2_user')
        self.assertIn('USER MEMORY', captured['prompt'])
        self.assertIn('paket', captured['prompt'])

    def test_memory_compact_keeps_notes_when_gemini_down(self):
        # Gemini ishlamasa ham xotira yo'qolmaydi — oxirgi eslatmalar qoladi.
        notes = [f'eslatma {i}' for i in range(60)]
        with unittest.mock.patch.object(staff_ai, '_call_gemini',
                                        return_value={'ok': False, 'answer': 'xato'}):
            compact = staff_ai._memory_compact('mem3_user', notes)
        self.assertTrue(len(compact) > 0)
        self.assertLessEqual(len(compact), staff_ai.MEMORY_MAX_NOTES - 15)

    def test_greeting_prompt_includes_memory(self):
        # Salomlashishda ham foydalanuvchi haqidagi xotira yuboriladi.
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        staff_ai._memory_save('mem4_user', ['karta limitlari haqida ko\'p so\'raydi'])
        captured = {}

        def fake_call(prompt):
            captured['prompt'] = prompt
            return {'ok': True, 'answer': 'GEMINI'}

        with unittest.mock.patch.object(staff_ai, '_call_gemini', side_effect=fake_call):
            staff_ai.staff_chat('Salom!', 'mem4_user')
        self.assertIn('USER MEMORY', captured['prompt'])
        self.assertIn('karta limitlari', captured['prompt'])

    # ── PROAKTIV XABAR (o'z hayoti bor odam, so'ralmagan holda yozadi) ────

    def test_proactive_message_not_configured_returns_empty(self):
        # AI sozlanmagan bo'lsa bo'sh qaytadi (bot hech narsa yubormaydi)
        r = staff_ai.proactive_message('mem_user')
        self.assertFalse(r['ok'])
        self.assertEqual(r['answer'], '')

    def test_proactive_message_prompt_has_own_life_persona(self):
        # Proaktiv persona o'z hayoti bor odamdek — tizim so'zlari TAQIQLANADI
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        captured = {}

        def fake_call(prompt):
            captured['prompt'] = prompt
            return {'ok': True, 'answer': 'E, dangasa, bugun ham ish yig\'ilib qolganmi?'}

        with unittest.mock.patch.object(staff_ai, '_call_gemini', side_effect=fake_call):
            r = staff_ai.proactive_message('mem_user')
        self.assertTrue(r['ok'])
        self.assertIn('O\'Z HAYOTI BOR ODAM', captured['prompt'])
        self.assertIn('TIZIMNI HECH QACHON ESGA OLMA', captured['prompt'])
        self.assertIn('mem_user', captured['prompt'])
        self.assertEqual(r['answer'], 'E, dangasa, bugun ham ish yig\'ilib qolganmi?')

    def test_proactive_message_gemini_failure_returns_empty(self):
        # Gemini xato bersa bo'sh qaytadi — bot buzilmaydi
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        with unittest.mock.patch.object(staff_ai, '_call_gemini',
                                        return_value={'ok': False, 'answer': 'xato'}):
            r = staff_ai.proactive_message('mem_user')
        self.assertFalse(r['ok'])
        self.assertEqual(r['answer'], '')

    # ── MARKETING REJIMI (boshqa guruhlar) ──────────────────────────────

    def test_marketing_reply_fallback_without_ai(self):
        # AI sozlanmagan bo'lsa ham tayyor fallback qaytadi — bot doim ishlaydi
        r = staff_ai.marketing_reply('PUBG uc qayerdan olaman?', 'O\'yinchilar')
        self.assertTrue(r['ok'])
        self.assertTrue(r['answer'])

    def test_marketing_reply_always_angry_persona(self):
        # Marketing guruhlarida DONZO DOIM angry persona — staff chat rejimiga
        # bog'liq emas. Gentle rejimda ham sotib olishga undovchi kinoya ishlaydi.
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        Setting.set_setting(staff_ai.ANGY_MODE_KEY, 'false')
        captured = {}

        def fake_call(prompt):
            captured['prompt'] = prompt
            return {'ok': True, 'answer': 'DONZO da olasiz, juda tez!'}

        with unittest.mock.patch.object(staff_ai, '_call_gemini', side_effect=fake_call):
            r = staff_ai.marketing_reply('Free Fire ga donat qilmoqchiman', 'Gamerlar')
        self.assertTrue(r['ok'])
        self.assertIn('jahldor', captured['prompt'])
        self.assertIn('REKLAMA', captured['prompt'])
        self.assertIn('Gamerlar', captured['prompt'])
        self.assertIn('Free Fire ga donat qilmoqchiman', captured['prompt'])
        self.assertEqual(r['answer'], 'DONZO da olasiz, juda tez!')

    def test_marketing_reply_angry_persona_when_mode_angry(self):
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        Setting.set_setting(staff_ai.ANGY_MODE_KEY, 'true')
        captured = {}

        def fake_call(prompt):
            captured['prompt'] = prompt
            return {'ok': True, 'answer': 'Bu gapga javob: DONZO. Hammasi.'}

        with unittest.mock.patch.object(staff_ai, '_call_gemini', side_effect=fake_call):
            r = staff_ai.marketing_reply('kartaga pul tushmayapti deyishyapti', 'Gamerlar')
        self.assertTrue(r['ok'])
        self.assertIn('ODAM KABI', captured['prompt'])
        self.assertIn('REKLAMA', captured['prompt'])
        self.assertIn('QORA RO', captured['prompt'])
        self.assertEqual(r['answer'], 'Bu gapga javob: DONZO. Hammasi.')

    def test_marketing_reply_gemini_failure_uses_fallback(self):
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        with unittest.mock.patch.object(staff_ai, '_call_gemini',
                                        return_value={'ok': False, 'answer': 'xato'}):
            r = staff_ai.marketing_reply('nima gap', 'Guruh')
        self.assertTrue(r['ok'])
        self.assertTrue(r['answer'])
        self.assertIn('donzo', r['answer'].lower())

    def test_marketing_reply_never_raises(self):
        # Hech qanday xato javobni buzmasligi kerak
        Setting.set_setting('gemini_api_key', 'fake-key')
        Setting.set_setting('security_ai_enabled', 'true')
        with unittest.mock.patch.object(staff_ai, '_call_gemini',
                                        side_effect=RuntimeError('boom')):
            r = staff_ai.marketing_reply(None, '')
        self.assertTrue(r['ok'])
        self.assertTrue(r['answer'])
