from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from catalog.models import Ingredient, MenuItem, Promotion
from catalog.services import PromotionService
from pos.models import SaleItem
from users.models import CustomUser, Institution, ParentProfile, StudentProfile

PROMOTIONS = '/api/catalog/promotions/'
ACTIVE_PROMOTIONS = '/api/catalog/promotions/active/'
TODAY = date(2026, 7, 6)
GIF = (
    b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04'
    b'\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
)


@mock.patch.object(PromotionService, 'today', return_value=TODAY)
class PromotionTests(APITestCase):
    """HU-03.3: percentage promotions over catalog products."""

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

        self.ingredient = Ingredient.objects.create(name='Base promo', allergens_reviewed=True)
        self.sandwich = self._item(self.inst_a, 'Sandwich', '2.50')
        self.juice = self._item(self.inst_a, 'Jugo', '1.00')
        self.item_b = self._item(self.inst_b, 'Pan B', '1.00')

    def _item(self, institution, name, price):
        item = MenuItem.objects.create(institution=institution, name=name, price=Decimal(price), stock=10)
        item.ingredients.add(self.ingredient)
        return item

    def _auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _payload(self, products, start=TODAY, days=7, discount=20):
        return {
            'products': [p.id for p in products],
            'discount_percentage': discount,
            'start_date': start.isoformat(),
            'end_date': (start + timedelta(days=days)).isoformat(),
        }

    def _promotion(self, products, start=TODAY, days=7, discount=20, institution=None):
        promotion = Promotion.objects.create(
            institution=institution or self.inst_a,
            discount_percentage=discount,
            start_date=start,
            end_date=start + timedelta(days=days),
        )
        promotion.products.set(products)
        return promotion

    # ---- creation -----------------------------------------------------------
    def test_admin_creates_promotion_for_own_institution(self, _today):
        self._auth(self.admin_a)
        response = self.client.post(PROMOTIONS, self._payload([self.sandwich, self.juice]), format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['status'], Promotion.Status.ACTIVE)
        self.assertEqual({p['name'] for p in response.data['products']}, {'Sandwich', 'Jugo'})
        promotion = Promotion.objects.get()
        self.assertEqual((promotion.institution, promotion.created_by), (self.inst_a, self.admin_a))

    def test_product_with_active_promotion_is_blocked(self, _today):
        self._promotion([self.sandwich])
        self._auth(self.admin_a)
        response = self.client.post(PROMOTIONS, self._payload([self.sandwich, self.juice]), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Sandwich', str(response.data['products']))
        self.assertEqual(Promotion.objects.count(), 1)

    def test_product_can_be_promoted_again_after_the_previous_promotion(self, _today):
        self._promotion([self.sandwich], days=3)
        self._auth(self.admin_a)
        later = TODAY + timedelta(days=4)
        response = self.client.post(PROMOTIONS, self._payload([self.sandwich], start=later), format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['status'], Promotion.Status.SCHEDULED)

    def test_editing_a_promotion_does_not_conflict_with_itself(self, _today):
        promotion = self._promotion([self.sandwich])
        self._auth(self.admin_a)
        response = self.client.patch(
            f'{PROMOTIONS}{promotion.id}/', {'discount_percentage': 30}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['discount_percentage'], 30)

    def test_invalid_data_is_rejected(self, _today):
        self._auth(self.admin_a)
        cases = [
            self._payload([]),
            self._payload([self.sandwich], discount=0),
            self._payload([self.sandwich], discount=100),
            self._payload([self.sandwich], days=-1),
            self._payload([self.sandwich], start=TODAY - timedelta(days=10), days=2),
            self._payload([self.item_b]),
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post(PROMOTIONS, payload, format='json').status_code, 400)

    def test_only_admins_manage_promotions(self, _today):
        self._auth(self.staff_a)
        self.assertEqual(self.client.get(PROMOTIONS).status_code, 403)
        self.assertEqual(self.client.post(PROMOTIONS, self._payload([self.sandwich]), format='json').status_code, 403)

    def test_admin_only_sees_own_institution_promotions(self, _today):
        self._promotion([self.sandwich])
        self._promotion([self.item_b], institution=self.inst_b)
        self._auth(self.admin_b)
        response = self.client.get(PROMOTIONS)
        rows = response.data['results'] if isinstance(response.data, dict) else response.data
        self.assertEqual([p['products'][0]['name'] for p in rows], ['Pan B'])

    # ---- price ----------------------------------------------------------------
    def test_catalog_shows_discounted_price_only_while_in_force(self, today):
        self._promotion([self.sandwich], days=3, discount=20)
        self._auth(self.admin_a)

        rows = {p['name']: p for p in self.client.get('/api/catalog/menu/admin/').data}
        self.assertEqual((rows['Sandwich']['discount_percentage'], rows['Sandwich']['final_price']), (20, '2.00'))
        self.assertEqual((rows['Jugo']['discount_percentage'], rows['Jugo']['final_price']), (None, '1.00'))

        today.return_value = TODAY + timedelta(days=4)
        rows = {p['name']: p for p in self.client.get('/api/catalog/menu/admin/').data}
        self.assertEqual((rows['Sandwich']['discount_percentage'], rows['Sandwich']['final_price']), (None, '2.50'))
        self.sandwich.refresh_from_db()
        self.assertEqual(self.sandwich.price, Decimal('2.50'))

    def test_pos_charges_discounted_price_and_reverts_after_expiry(self, today):
        self._promotion([self.sandwich], days=3, discount=20)
        self._auth(self.staff_a)
        sale = {'payment_method': 'CASH', 'items': [{'menu_item_id': self.sandwich.id, 'quantity': 2}]}

        response = self.client.post('/api/pos/sales/', sale, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['total_amount'], '4.00')
        self.assertEqual(SaleItem.objects.get().price_at_purchase, Decimal('2.00'))

        today.return_value = TODAY + timedelta(days=4)
        response = self.client.post('/api/pos/sales/', sale, format='json')
        self.assertEqual(response.data['total_amount'], '5.00')

    # ---- mobile banner --------------------------------------------------------
    def test_parent_sees_only_promotions_in_force_of_child_institution(self, _today):
        parent_user = CustomUser.objects.create_user(
            username='parent', email='p@x.com', password='x', role=CustomUser.Role.PARENT)
        parent = ParentProfile.objects.create(user=parent_user)
        student_user = CustomUser.objects.create_user(
            username='student', email='st@x.com', password='x', role=CustomUser.Role.STUDENT)
        student = StudentProfile.objects.create(
            user=student_user, parent=parent, institution=self.inst_a,
            profile_picture=SimpleUploadedFile('s.gif', GIF, content_type='image/gif'))
        active = self._promotion([self.sandwich], days=3)
        self._promotion([self.juice], start=TODAY + timedelta(days=5))
        self._promotion([self.juice], start=TODAY - timedelta(days=10), days=2)
        self._promotion([self.item_b], institution=self.inst_b)

        self._auth(parent_user)
        self.assertEqual(self.client.get(ACTIVE_PROMOTIONS).status_code, 400)
        response = self.client.get(f'{ACTIVE_PROMOTIONS}?student_id={student.id}')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([p['id'] for p in response.data], [active.id])

        self._auth(student_user)
        self.assertEqual([p['id'] for p in self.client.get(ACTIVE_PROMOTIONS).data], [active.id])
