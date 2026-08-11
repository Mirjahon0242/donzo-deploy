"""Tests for the Fragment live-price sync module (fragment-api.uz API)."""

from unittest import mock

from django.test import TestCase

from apps.services.models import Category, Service, Package
from apps.settings_app.models import Setting


def _make_telegram_premium():
    cat, _ = Category.objects.get_or_create(slug='social', defaults={'name': 'Social'})
    svc, _ = Service.objects.get_or_create(
        slug='telegram-premium',
        defaults={'name': 'Telegram Premium', 'category': cat},
    )
    svc.category = cat
    svc.save()
    Package.objects.update_or_create(service=svc, name='3 oy Premium', defaults={'price': 100000})
    Package.objects.update_or_create(service=svc, name='100 Stars', defaults={'price': 10000})
    Package.objects.update_or_create(service=svc, name='1 oy Premium', defaults={'price': 45000})
    return svc


MOCK_PREMIUM = {
    'packages': [
        {'months': 3, 'ton': '5', 'usd': '11.99'},
        {'months': 6, 'ton': '6.67', 'usd': '15.99'},
        {'months': 12, 'ton': '12.1', 'usd': '28.99'},
    ],
}

MOCK_STARS = {
    'amount': 100,
    'price': {'ton': '0.6494', 'usd': '1.50', 'selected': '1.50'},
}


class FragmentPriceSyncTests(TestCase):
    def setUp(self):
        _make_telegram_premium()
        Setting.set_setting('fragment_usd_uzs_rate', '12800')
        Setting.set_setting('fragment_price_margin_percent', '15')
        Setting.set_setting('fragment_api_key', 'test-key-123')

    @mock.patch('apps.services.fragment_api.get_premium_pricing', return_value=MOCK_PREMIUM)
    @mock.patch('apps.services.fragment_api.get_stars_price', return_value=MOCK_STARS)
    def test_updates_live_prices(self, mock_stars, mock_premium):
        from apps.services.fragment_price_sync import sync_fragment_prices

        result = sync_fragment_prices(force=True)

        self.assertEqual(result['errors'], 0)
        self.assertEqual(result['updated'], 2)

        # 3 oy Premium: 11.99 * 12800 * 1.15 = 176,492.8 -> 177,000
        pkg3 = Package.objects.get(service__slug='telegram-premium', name='3 oy Premium')
        self.assertEqual(int(pkg3.price), 177000)

        # 100 Stars: 1.50 * 12800 * 1.15 = 22,080 -> 23,000
        pkg100 = Package.objects.get(service__slug='telegram-premium', name='100 Stars')
        self.assertEqual(int(pkg100.price), 23000)
        # Stars narxi har paket uchun so'raladi
        mock_stars.assert_called_once_with(100, timeout=None)

        # '1 oy Premium' — fragment API faqat 3/6/12 qo'llab-quvvatlaydi -> tegilmaydi
        pkg1 = Package.objects.get(service__slug='telegram-premium', name='1 oy Premium')
        self.assertEqual(int(pkg1.price), 45000)

        # Last-sync timestamp written
        self.assertTrue(Setting.get_setting('fragment_last_price_sync', ''))

    @mock.patch('apps.services.fragment_api.get_premium_pricing', return_value=MOCK_PREMIUM)
    @mock.patch('apps.services.fragment_api.get_stars_price', return_value=MOCK_STARS)
    def test_skips_within_24h(self, mock_stars, mock_premium):
        from django.utils import timezone
        from apps.services.fragment_price_sync import sync_fragment_prices

        Setting.set_setting('fragment_last_price_sync', timezone.now().isoformat())
        result = sync_fragment_prices(force=False)

        self.assertFalse(result['synced'])
        self.assertEqual(result['updated'], 0)
        mock_premium.assert_not_called()
        mock_stars.assert_not_called()

    @mock.patch('apps.services.fragment_api.get_premium_pricing',
                side_effect=Exception('API down'))
    def test_never_raises_on_api_error(self, mock_premium):
        from apps.services.fragment_price_sync import sync_fragment_prices

        result = sync_fragment_prices(force=True)  # must not raise
        self.assertIn('xatolik', result['result'].lower())

    def test_parse_helpers(self):
        from apps.services.fragment_price_sync import _parse_stars_amount, _parse_premium_months
        self.assertEqual(_parse_stars_amount('100 Stars'), 100)
        self.assertEqual(_parse_stars_amount('50 stars'), 50)
        self.assertEqual(_parse_premium_months('3 oy Premium'), 3)
        self.assertEqual(_parse_premium_months('12 oy'), 12)
        self.assertIsNone(_parse_stars_amount('1 oy Premium'))
        self.assertIsNone(_parse_premium_months('100 Stars'))


class FragmentApiClientTests(TestCase):
    """Fragment API client (fragment-api.uz) — auth va javob format testlari."""

    def setUp(self):
        Setting.set_setting('fragment_api_key', 'test-key-123')
        Setting.set_setting('fragment_api_base_url', 'https://fragment-api.uz/api/v1')

    def test_missing_api_key_raises(self):
        Setting.set_setting('fragment_api_key', '')
        from apps.services.fragment_api import FragmentAPIError, get_stars_price
        with self.assertRaises(FragmentAPIError) as ctx:
            get_stars_price(60)
        self.assertEqual(ctx.exception.error_code, 'API_KEY_MISSING')

    def test_sends_api_key_header(self):
        from apps.services import fragment_api
        with mock.patch('apps.services.fragment_api.requests.post') as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {
                'ok': True,
                'result': {'amount': 60, 'price': {'usd': '0.90'}},
            }
            fragment_api.get_stars_price(60)
            _, kwargs = mock_post.call_args
            self.assertEqual(kwargs['headers']['X-API-Key'], 'test-key-123')

    def test_ok_false_raises_with_code(self):
        from apps.services.fragment_api import FragmentAPIError, get_premium_pricing
        with mock.patch('apps.services.fragment_api.requests.post') as mock_post:
            mock_post.return_value.status_code = 401
            mock_post.return_value.json.return_value = {
                'ok': False,
                'message': "X-API-Key maydoni noto'g'ri",
                'code': 'VALIDATION_ERROR',
            }
            with self.assertRaises(FragmentAPIError) as ctx:
                get_premium_pricing()
            self.assertEqual(ctx.exception.error_code, 'INVALID_API_KEY')

    def test_buy_premium_invalid_duration(self):
        from apps.services.fragment_api import FragmentAPIError, buy_premium
        with self.assertRaises(FragmentAPIError) as ctx:
            buy_premium('@test', 5)
        self.assertEqual(ctx.exception.error_code, 'INVALID_DURATION')

    def test_buy_stars_below_minimum(self):
        from apps.services.fragment_api import FragmentAPIError, buy_stars
        with self.assertRaises(FragmentAPIError) as ctx:
            buy_stars('@test', 10)
        self.assertEqual(ctx.exception.error_code, 'INVALID_AMOUNT')
