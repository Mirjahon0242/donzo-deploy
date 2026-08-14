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

    # ── MAXSUS STSENARIYLAR ────────────────────────────────────────────────
    def _mk_user(self, username, role):
        from apps.users.models import User
        return User.objects.create_user(username=username, email=f'{username}@t.uz',
                                        password='x12345678', role=role)

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
