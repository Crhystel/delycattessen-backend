import json
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from catalog.models import Combo, ComboItem, Ingredient, MenuItem
from catalog.services import ComboService
from users.models import CustomUser, Institution, ParentProfile, StudentProfile

COMBOS = '/api/catalog/combos/'
ACTIVE_COMBOS = '/api/catalog/combos/active/'
TODAY = date(2026, 7, 6)
GIF = (
    b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04'
    b'\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
)


def _image(name='combo.gif'):
    return SimpleUploadedFile(name, GIF, content_type='image/gif')


@mock.patch.object(ComboService, 'today', return_value=TODAY)
class ComboTests(APITestCase):
    """Fixed-price combos of catalog products (e.g. sandwich + cola for $3.50)."""

    def setUp(self):
        make = CustomUser.objects.create_user
        self.inst_a = Institution.objects.create(name='Sede A')
        self.inst_b = Institution.objects.create(name='Sede B')
        self.admin_a = make(username='admin_a', email='aa@x.com', password='x',
                            role=CustomUser.Role.ADMIN, institution=self.inst_a)
        self.admin_b = make(username='admin_b', email='ab@x.com', password='x',
                            role=CustomUser.Role.ADMIN, institution=self.inst_b)
        self.staff_a = make(username='staff_a', email='sa@x.com', password='x',
                            role=CustomUser.Role.OPERATIONS_STAFF, institution=self.inst_a)

        self.ingredient = Ingredient.objects.create(name='Base combo', allergens_reviewed=True)
        self.sandwich = self._item(self.inst_a, 'Sanduche de pollo', '2.50')
        self.cola = self._item(self.inst_a, 'Cola', '1.50')
        self.item_b = self._item(self.inst_b, 'Pan B', '1.00')

    def _item(self, institution, name, price):
        item = MenuItem.objects.create(institution=institution, name=name, price=Decimal(price), stock=10)
        item.ingredients.add(self.ingredient)
        return item

    def _auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _payload(self, items, start=TODAY, days=7, price='3.50', image=True):
        payload = {
            'name': 'Combo almuerzo',
            'price': price,
            'start_date': start.isoformat(),
            'end_date': (start + timedelta(days=days)).isoformat(),
            'items': json.dumps([{'product_id': p.id, 'quantity': q} for p, q in items]),
        }
        if image:
            payload['image'] = _image()
        return payload

    def _combo(self, items, start=TODAY, days=7, institution=None, price='3.50'):
        combo = Combo.objects.create(
            institution=institution or self.inst_a, name='Combo', price=Decimal(price),
            start_date=start, end_date=start + timedelta(days=days), image=_image(),
        )
        for product, quantity in items:
            ComboItem.objects.create(combo=combo, menu_item=product, quantity=quantity)
        return combo

    # ---- admin --------------------------------------------------------------
    def test_admin_creates_combo_with_products_and_image(self, _today):
        self._auth(self.admin_a)
        response = self.client.post(COMBOS, self._payload([(self.sandwich, 1), (self.cola, 2)]), format='multipart')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['status'], Combo.Status.ACTIVE)
        self.assertEqual(response.data['price'], '3.50')
        self.assertEqual(response.data['regular_price'], '5.50')
        self.assertEqual(
            {(row['name'], row['quantity']) for row in response.data['items']},
            {('Sanduche de pollo', 1), ('Cola', 2)},
        )
        combo = Combo.objects.get()
        self.assertEqual((combo.institution, combo.created_by), (self.inst_a, self.admin_a))

    def test_invalid_combos_are_rejected(self, _today):
        self._auth(self.admin_a)
        cases = [
            self._payload([]),
            self._payload([(self.sandwich, 1), (self.sandwich, 1)]),
            self._payload([(self.item_b, 1)]),
            self._payload([(self.sandwich, 0)]),
            self._payload([(self.sandwich, 1)], price='0'),
            self._payload([(self.sandwich, 1)], days=-1),
            self._payload([(self.sandwich, 1)], start=TODAY - timedelta(days=10), days=2),
            self._payload([(self.sandwich, 1)], image=False),
        ]
        for payload in cases:
            with self.subTest(payload={k: v for k, v in payload.items() if k != 'image'}):
                self.assertEqual(self.client.post(COMBOS, payload, format='multipart').status_code, 400)
        self.assertFalse(Combo.objects.exists())

    def test_editing_replaces_products_and_keeps_image(self, _today):
        combo = self._combo([(self.sandwich, 1)])
        self._auth(self.admin_a)
        response = self.client.patch(
            f'{COMBOS}{combo.id}/',
            {'price': '3.00', 'items': json.dumps([{'product_id': self.cola.id, 'quantity': 2}])},
            format='multipart',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([(row['name'], row['quantity']) for row in response.data['items']], [('Cola', 2)])
        self.assertEqual(response.data['price'], '3.00')
        self.assertTrue(response.data['image'])

    def test_only_admins_of_the_institution_manage_combos(self, _today):
        self._combo([(self.sandwich, 1)])
        self._auth(self.staff_a)
        self.assertEqual(self.client.get(COMBOS).status_code, 403)
        self._auth(self.admin_b)
        self.assertEqual(self.client.get(COMBOS).data, [])

    # ---- banner (app and POS) -------------------------------------------------
    def test_active_combos_are_only_those_in_force_of_the_viewer_institution(self, _today):
        active = self._combo([(self.sandwich, 1), (self.cola, 1)], days=3)
        self._combo([(self.cola, 1)], start=TODAY + timedelta(days=5))
        self._combo([(self.cola, 1)], start=TODAY - timedelta(days=10), days=2)
        self._combo([(self.item_b, 1)], institution=self.inst_b)

        self._auth(self.staff_a)
        self.assertEqual([c['id'] for c in self.client.get(ACTIVE_COMBOS).data], [active.id])

        parent_user = CustomUser.objects.create_user(
            username='parent', email='p@x.com', password='x', role=CustomUser.Role.PARENT)
        parent = ParentProfile.objects.create(user=parent_user)
        student_user = CustomUser.objects.create_user(
            username='student', email='st@x.com', password='x', role=CustomUser.Role.STUDENT)
        student = StudentProfile.objects.create(
            user=student_user, parent=parent, institution=self.inst_a, profile_picture=_image('s.gif'))
        self._auth(parent_user)
        response = self.client.get(f'{ACTIVE_COMBOS}?student_id={student.id}')
        self.assertEqual([c['id'] for c in response.data], [active.id])
