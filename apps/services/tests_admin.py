"""
Admin service management tests.

  • GET  /api/v1/admin/services/<id>/  — to'liq xizmat: BARCHA paketlar va
    maydonlar bilan (admin tahrirlash formasi to'liq ma'lumot oladi).
  • Yangi xizmatda slug avtomatik generatsiya qilinadi.
  • Paket va maydonlar alohida admin endpoint orqali yaratiladi/o'chiriladi.
"""
from django.test import TestCase
from rest_framework.test import APIClient

from apps.users.models import User
from apps.services.models import Category, Service, Package, ServiceField


def make_admin():
    return User.objects.create(
        username='svc_admin', email='svc_admin@tg.user',
        role='admin', is_active=True,
    )


def make_category():
    cat, _ = Category.objects.get_or_create(
        slug='games', defaults={'name': 'Games', 'is_active': True},
    )
    return cat


class AdminServiceDetailTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)
        self.cat = make_category()

    def _make_service(self):
        svc = Service.objects.create(name='Mobile Legends', category=self.cat)
        Package.objects.create(service=svc, name='86 Diamond', price=15000)
        Package.objects.create(service=svc, name='172 Diamond', price=29000, is_active=False)
        ServiceField.objects.create(service=svc, field_name='game_id', field_label='Game ID', is_required=True)
        return svc

    def test_admin_detail_returns_all_packages_and_fields(self):
        svc = self._make_service()
        res = self.client.get(f'/api/v1/admin/services/{svc.id}/')
        self.assertEqual(res.status_code, 200)
        data = res.data
        # Barcha paketlar — nofaol ham (admin tahrirlash uchun)
        names = [p['name'] for p in data['packages']]
        self.assertIn('86 Diamond', names)
        self.assertIn('172 Diamond', names)  # nofaol ham ko'rinadi
        self.assertEqual(len(data['packages']), 2)
        # Maydonlar
        self.assertEqual(len(data['fields']), 1)
        self.assertEqual(data['fields'][0]['field_label'], 'Game ID')
        self.assertTrue(data['fields'][0]['is_required'])

    def test_create_service_auto_generates_slug(self):
        res = self.client.post('/api/v1/admin/services/', {
            'name': 'Call of Duty',
            'category': self.cat.id,
            'is_active': True,
        }, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data['slug'], 'call-of-duty')
        svc = Service.objects.get(pk=res.data['id'])
        self.assertEqual(svc.slug, 'call-of-duty')

    def test_update_service_keeps_packages(self):
        svc = self._make_service()
        res = self.client.put(f'/api/v1/admin/services/{svc.id}/', {
            'name': 'MLBB',
            'category': self.cat.id,
            'is_active': True,
        }, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['name'], 'MLBB')
        self.assertEqual(len(res.data['packages']), 2)  # paketlar saqlanadi

    def test_package_crud(self):
        svc = self._make_service()
        # create
        res = self.client.post('/api/v1/admin/packages/', {
            'service': svc.id, 'name': '257 Diamond', 'price': 43000, 'currency': 'UZS', 'is_active': True,
        }, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        pkg_id = res.data['id']
        # delete
        res = self.client.delete(f'/api/v1/admin/packages/{pkg_id}/')
        self.assertEqual(res.status_code, 204)
        self.assertFalse(Package.objects.filter(pk=pkg_id).exists())

    def test_field_crud(self):
        svc = self._make_service()
        res = self.client.post('/api/v1/admin/fields/', {
            'service': svc.id, 'field_name': 'server_id', 'field_label': 'Server ID',
            'field_type': 'number', 'is_required': False,
        }, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        fld_id = res.data['id']
        res = self.client.delete(f'/api/v1/admin/fields/{fld_id}/')
        self.assertEqual(res.status_code, 204)
        self.assertFalse(ServiceField.objects.filter(pk=fld_id).exists())

    def test_admin_required(self):
        customer = User.objects.create(username='svc_cust', email='svc_cust@tg.user', role='customer')
        client = APIClient()
        client.force_authenticate(user=customer)
        svc = self._make_service()
        res = client.get(f'/api/v1/admin/services/{svc.id}/')
        self.assertIn(res.status_code, (401, 403))
