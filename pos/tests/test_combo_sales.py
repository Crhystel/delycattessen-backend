from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from catalog.models import Allergen, Combo, ComboItem, Ingredient, MenuItem
from catalog.services import ComboService
from pos.models import Sale, SaleCombo
from users.models import CustomUser, Institution, ParentProfile, StudentProfile
from wallet.models import Wallet

GIF = (
    b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04'
    b'\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
)
SALES = '/api/pos/sales/'
TODAY = date(2026, 7, 6)


def _image(name):
    return SimpleUploadedFile(name, GIF, content_type='image/gif')


@mock.patch.object(ComboService, 'today', return_value=TODAY)
class ComboSaleTests(APITestCase):
    """A combo is one line at its fixed price; its products still use stock."""

    def setUp(self):
        make = CustomUser.objects.create_user
        self.inst_a = Institution.objects.create(name='Sede A')
        self.inst_b = Institution.objects.create(name='Sede B')
        self.staff_a = make(username='staff_a', email='sa@x.com', password='x',
                            role=CustomUser.Role.OPERATIONS_STAFF, institution=self.inst_a)
        parent = ParentProfile.objects.create(
            user=make(username='parent', email='p@x.com', password='x', role=CustomUser.Role.PARENT))
        self.student_user = make(username='student', email='st@x.com', password='x', role=CustomUser.Role.STUDENT)
        self.student = StudentProfile.objects.create(
            user=self.student_user, parent=parent, institution=self.inst_a, profile_picture=_image('s.gif'))
        self.wallet = Wallet.objects.create(
            student=self.student, balance=Decimal('10.00'), low_balance_threshold=Decimal('1.00'))

        ingredient = Ingredient.objects.create(name='Base', allergens_reviewed=True)
        self.sandwich = self._item('Sanduche de pollo', '2.50', ingredient)
        self.cola = self._item('Cola', '1.50', ingredient)
        self.combo = self._combo(self.inst_a, [(self.sandwich, 1), (self.cola, 1)])

    def _item(self, name, price, ingredient, stock=5):
        item = MenuItem.objects.create(institution=self.inst_a, name=name, price=Decimal(price), stock=stock)
        item.ingredients.add(ingredient)
        return item

    def _combo(self, institution, items, start=TODAY, days=7):
        combo = Combo.objects.create(
            institution=institution, name='Combo almuerzo', price=Decimal('3.50'),
            start_date=start, end_date=start + timedelta(days=days), image=_image('c.gif'),
        )
        for product, quantity in items:
            ComboItem.objects.create(combo=combo, menu_item=product, quantity=quantity)
        return combo

    def _auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _sell(self, method='CASH', combos=None, items=None, user=None):
        payload = {
            'payment_method': method,
            'items': items or [],
            'combos': combos if combos is not None else [{'combo_id': self.combo.id, 'quantity': 1}],
        }
        if user is not None:
            payload['user_id'] = user.id
        return self.client.post(SALES, payload, format='json')

    def _stock(self, item):
        item.refresh_from_db()
        return item.stock

    def test_combo_is_charged_at_its_price_and_discounts_each_product(self, _today):
        self._auth(self.staff_a)
        response = self._sell(combos=[{'combo_id': self.combo.id, 'quantity': 2}])
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['total_amount'], '7.00')
        self.assertEqual(response.data['items'], [
            {'menu_item_name': 'Combo almuerzo', 'quantity': 2, 'price_at_purchase': '3.50'},
        ])
        self.assertEqual((self._stock(self.sandwich), self._stock(self.cola)), (3, 3))
        line = SaleCombo.objects.get()
        self.assertEqual((line.combo, line.name, line.quantity), (self.combo, 'Combo almuerzo', 2))

    def test_combo_and_loose_products_in_the_same_sale(self, _today):
        self._auth(self.staff_a)
        response = self._sell(items=[{'menu_item_id': self.cola.id, 'quantity': 1}])
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['total_amount'], '5.00')
        self.assertEqual(self._stock(self.cola), 3)

    def test_combo_paid_with_wallet(self, _today):
        self._auth(self.staff_a)
        self.assertEqual(self._sell('WALLET', user=self.student_user).status_code, 201)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('6.50'))

    def test_stock_of_combo_products_is_checked(self, _today):
        self.cola.stock = 1
        self.cola.save()
        self._auth(self.staff_a)
        response = self._sell(combos=[{'combo_id': self.combo.id, 'quantity': 2}])
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Sale.objects.exists())

    def test_allergens_of_combo_products_block_the_sale(self, _today):
        allergen = Allergen.objects.create(name='Gluten combo')
        self.sandwich.allergens.add(allergen)
        self.student.allergies.add(allergen)
        self._auth(self.staff_a)
        self.assertEqual(self._sell('WALLET', user=self.student_user).status_code, 400)
        self.assertEqual(self._stock(self.sandwich), 5)

    def test_combo_not_in_force_or_of_another_institution_is_rejected(self, today):
        other = self._combo(self.inst_b, [])
        self._auth(self.staff_a)
        self.assertEqual(self._sell(combos=[{'combo_id': other.id, 'quantity': 1}]).status_code, 400)
        today.return_value = TODAY + timedelta(days=8)
        self.assertEqual(self._sell().status_code, 400)
        self.assertFalse(Sale.objects.exists())

    def test_sale_needs_a_product_or_a_combo(self, _today):
        self._auth(self.staff_a)
        self.assertEqual(self._sell(combos=[]).status_code, 400)

    def test_receipt_survives_deleting_the_combo(self, _today):
        self._auth(self.staff_a)
        self.assertEqual(self._sell().status_code, 201)
        self.combo.delete()
        line = SaleCombo.objects.get()
        self.assertEqual((line.combo, line.name), (None, 'Combo almuerzo'))
