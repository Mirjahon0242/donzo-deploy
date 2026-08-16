# -*- coding: utf-8 -*-
"""gemini_client rotatsiya testlari — 429 quota himoyasi."""
import unittest
from unittest import mock

from apps.security import gemini_client


class GeminiClientTests(unittest.TestCase):

    def setUp(self):
        gemini_client._quota_cooldown_until.clear()

    def test_model_order_configured_first(self):
        order = gemini_client._model_order('gemini-x-test')
        self.assertEqual(order[0], 'gemini-x-test')
        self.assertIn('gemini-3.6-flash', order)

    def test_quota_cooldown_skips_tired_model(self):
        gemini_client._mark_quota('gemini-3.6-flash', seconds=1000)
        order = gemini_client._model_order('gemini-3.6-flash')
        # Charchagan model oxirga tushadi, lekin butunlay tashlanmaydi
        self.assertNotEqual(order[0], 'gemini-3.6-flash')
        self.assertIn('gemini-3.6-flash', order)

    def test_chat_rotates_on_429(self):
        """Birinchi model 429 bersa — keyingi modelga o'tadi va javob qaytadi."""
        calls = []

        def fake_post(url, body, timeout=45):
            calls.append(url)
            if 'gemini-3.6-flash' in url:
                return 429, '{"error":{"message":"Quota exceeded ... retry in 53.17s"}}'
            return 200, '{"candidates":[{"content":{"parts":[{"text":"salom donzo"}]}}]}'

        with mock.patch.object(gemini_client, '_post', side_effect=fake_post):
            res = gemini_client.chat('salom', configured_model='gemini-3.6-flash')
        self.assertTrue(res['ok'])
        self.assertEqual(res['answer'], 'salom donzo')
        self.assertGreater(len(calls), 1)
        # 429 bergan model cooldown'ga tushgan
        self.assertIn('gemini-3.6-flash', gemini_client._quota_cooldown_until)

    def test_chat_all_quota_returns_friendly(self):
        """Barcha modellar 429 bersa — aniq, foydalanuvchiga tushunarli javob."""
        def fake_post(url, body, timeout=45):
            return 429, '{"error":{"message":"Quota exceeded ... retry in 5s"}}'

        with mock.patch.object(gemini_client, '_post', side_effect=fake_post):
            res = gemini_client.chat('salom', configured_model='gemini-3.6-flash')
        self.assertFalse(res['ok'])
        self.assertIn('quota', res.get('answer', '').lower())

    def test_parse_retry_seconds(self):
        body = '{"error":{"message":"Quota exceeded ... Please retry in 53.17s."}}'
        self.assertAlmostEqual(gemini_client._parse_retry_seconds(body), 53.17, places=1)
        # xato body -> default cooldown
        self.assertEqual(gemini_client._parse_retry_seconds('not json'), gemini_client._QUOTA_COOLDOWN_SECONDS)


if __name__ == '__main__':
    unittest.main()
