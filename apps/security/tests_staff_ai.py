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
