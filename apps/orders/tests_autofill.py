"""Tests for order auto-fill (Telegram Premium username) + simplified fields."""

from django.test import TestCase
from rest_framework.test import APIClient

from apps.users.models import User, Role
from apps.services.models import Service, Category, Package, ServiceField


def make_telegram_service():
    cat, _ = Category.objects.get_or_create(slug='social', defaults={'name': 'Social'})
    svc, _ = Service.objects.get_or_create(
        slug='telegram-premium',
        defaults={'name': 'Telegram Premium', 'category': cat, 'is_active': True},
    )
    svc.category = cat
    svc.is_active = True
    svc.save()
    ServiceField.objects.get_or_create(
        service=svc, field_name='username',
        defaults={'field_label': 'Telegram Username', 'field_type': 'text',
                  'is_required': True, 'order_index': 1},
    )
    Package.objects.get_or_create(
        service=svc, name='3 oy Premium',
        defaults={'amount_label': '3 oy Premium', 'price': 125000, 'order_index': 1},
    )
    return svc


class TelegramPremiumAutoFillTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.svc = make_telegram_service()
        self.pkg = self.svc.packages.first()
        self.user = User.objects.create(
            username='tguser1', email='tguser1@telegram.user',
            telegram_id='777', telegram_username='my_tg_username',
            first_name='Test', role=Role.CUSTOMER,
        )

    def test_username_auto_filled_from_profile(self):
        """Foydalanuvchi username yubormasa ham u profilidan olinadi."""
        self.client.force_authenticate(self.user)
        resp = self.client.post('/api/v1/orders/', {
            'service': self.svc.id,
            'package': self.pkg.id,
            'field_values': {},
            'customer_name': 'Test',
            'customer_telegram': '@my_tg_username',
        }, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        order = resp.json()
        self.assertEqual(order['field_values']['username'], 'my_tg_username')

    def test_explicit_username_wins(self):
        """Foydalanuvchi o'zi yuborgan username ustun turadi."""
        self.client.force_authenticate(self.user)
        resp = self.client.post('/api/v1/orders/', {
            'service': self.svc.id,
            'package': self.pkg.id,
            'field_values': {'username': '@other_user'},
            'customer_name': 'Test',
            'customer_telegram': '@my_tg_username',
        }, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()['field_values']['username'], '@other_user')

    def test_no_username_no_profile_rejected(self):
        """Username yo'q va profilda ham yo'q bo'lsa — majburiy maydon xatosi."""
        user2 = User.objects.create(
            username='tguser2', email='tguser2@telegram.user',
            telegram_id='778', telegram_username='', role=Role.CUSTOMER,
        )
        self.client.force_authenticate(user2)
        resp = self.client.post('/api/v1/orders/', {
            'service': self.svc.id,
            'package': self.pkg.id,
            'field_values': {},
            'customer_name': 'Test',
            'customer_telegram': '@whatever',
        }, format='json')
        self.assertEqual(resp.status_code, 400)


class SimplifiedFieldsTests(TestCase):
    def test_pubg_requires_only_game_id(self):
        """PUBG: faqat game_id talab qilinadi — nickname endi maydon emas."""
        cat, _ = Category.objects.get_or_create(slug='mobile-games', defaults={'name': 'Mobile'})
        svc, _ = Service.objects.get_or_create(
            slug='pubg-mobile',
            defaults={'name': 'PUBG', 'category': cat, 'is_active': True},
        )
        svc.category = cat
        svc.save()
        ServiceField.objects.get_or_create(
            service=svc, field_name='game_id',
            defaults={'field_label': 'Game ID', 'field_type': 'number',
                      'is_required': True, 'order_index': 1},
        )
        # nickname maydoni mavjud emasligi — o'chirilgan
        self.assertFalse(
            ServiceField.objects.filter(service=svc, field_name='nickname').exists()
        )
        pkg, _ = Package.objects.get_or_create(
            service=svc, name='60 UC',
            defaults={'amount_label': '60 UC', 'price': 12000, 'order_index': 1},
        )

        user = User.objects.create(
            username='pubguser', email='pubguser@telegram.user',
            telegram_id='779', telegram_username='pubg_tg', role=Role.CUSTOMER,
        )
        client = APIClient()
        client.force_authenticate(user)
        resp = client.post('/api/v1/orders/', {
            'service': svc.id,
            'package': pkg.id,
            'field_values': {'game_id': '512345678'},
            'customer_name': 'Test',
            'customer_telegram': '@pubg_tg',
        }, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
