from datetime import timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from catalog.models import Allergen, DailyMenu, Ingredient, MenuDish, MenuItem, MonthlyMenu
from catalog.services import resync_allergens_for_ingredient, sync_allergens_from_ingredients
from core.timezone import local_today
from pos.models import PreOrder, Sale
from users.models import CustomUser, Institution, ParentProfile, StudentProfile
from wallet.models import Wallet

GIF = (
    b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04'
    b'\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
)


def _image(name):
    return SimpleUploadedFile(name, GIF, content_type='image/gif')


class IngredientAllergenSyncTests(APITestCase):
    """Products and dishes store a copy of their ingredients' allergens, which
    the POS and preorders check: editing an ingredient must keep it current."""

    def setUp(self):
        make = CustomUser.objects.create_user
        self.institution = Institution.objects.create(name='Sede A')
        self.admin = make(username='admin', email='a@x.com', password='x',
                          role=CustomUser.Role.ADMIN, institution=self.institution)
        self.staff = make(username='staff', email='s@x.com', password='x',
                          role=CustomUser.Role.OPERATIONS_STAFF, institution=self.institution)
        self.parent_user = make(username='parent', email='p@x.com', password='x', role=CustomUser.Role.PARENT)
        parent = ParentProfile.objects.create(user=self.parent_user)
        self.student_user = make(username='student', email='st@x.com', password='x',
                                 role=CustomUser.Role.STUDENT, institution=self.institution)
        self.student = StudentProfile.objects.create(
            user=self.student_user, parent=parent, institution=self.institution, profile_picture=_image('s.gif'))
        Wallet.objects.create(student=self.student, balance=Decimal('20.00'), low_balance_threshold=Decimal('1.00'))

        self.gluten = Allergen.objects.create(name='Gluten sync')
        self.soy = Allergen.objects.create(name='Soya sync')
        self.flour = Ingredient.objects.create(name='Harina sync', allergens_reviewed=True)
        self.flour.allergens.add(self.soy)
        self.rice = Ingredient.objects.create(name='Arroz sync', allergens_reviewed=True)

        self.item = MenuItem.objects.create(
            institution=self.institution, name='Pan', price=Decimal('1.00'), stock=10)
        self.item.ingredients.add(self.flour, self.rice)
        self.other_item = MenuItem.objects.create(
            institution=self.institution, name='Jugo', price=Decimal('1.00'), stock=10)
        self.other_item.ingredients.add(self.rice)
        self.dish = MenuDish.objects.create(
            institution=self.institution, name='Pasta', price=Decimal('3.00'), image=_image('d.gif'))
        self.dish.ingredients.add(self.flour)
        for obj in (self.item, self.other_item, self.dish):
            sync_allergens_from_ingredients(obj)

    def _auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _set_ingredient_allergens(self, ingredient, allergens):
        self._auth(self.admin)
        response = self.client.patch(
            f'/api/catalog/ingredients/{ingredient.id}/', {'allergens': [a.id for a in allergens]}, format='json')
        self.assertEqual(response.status_code, 200, response.data)

    @staticmethod
    def _allergens(obj):
        obj.refresh_from_db()
        return set(obj.allergens.all())

    def test_sync_is_the_union_of_the_ingredients_allergens(self):
        self.rice.allergens.add(self.gluten)
        sync_allergens_from_ingredients(self.item)
        self.assertEqual(self._allergens(self.item), {self.soy, self.gluten})

    def test_adding_an_allergen_to_an_ingredient_updates_products_and_dishes(self):
        self.assertEqual(self._allergens(self.item), {self.soy})
        self._set_ingredient_allergens(self.flour, [self.soy, self.gluten])
        self.assertEqual(self._allergens(self.item), {self.soy, self.gluten})
        self.assertEqual(self._allergens(self.dish), {self.soy, self.gluten})
        # A product that does not use the ingredient is left alone.
        self.assertEqual(self._allergens(self.other_item), set())

    def test_removing_an_allergen_from_an_ingredient_updates_products_and_dishes(self):
        self._set_ingredient_allergens(self.flour, [])
        self.assertEqual(self._allergens(self.item), set())
        self.assertEqual(self._allergens(self.dish), set())

    def test_allergen_stays_if_another_ingredient_still_has_it(self):
        self._set_ingredient_allergens(self.rice, [self.soy])
        self.assertEqual(self._allergens(self.other_item), {self.soy})
        self._set_ingredient_allergens(self.flour, [])
        self.assertEqual(self._allergens(self.item), {self.soy})  # still through the rice
        self.assertEqual(self._allergens(self.dish), set())

    def test_resync_for_an_ingredient_without_products_does_nothing(self):
        lonely = Ingredient.objects.create(name='Sal sync', allergens_reviewed=True)
        resync_allergens_for_ingredient(lonely)
        self.assertEqual(self._allergens(self.item), {self.soy})

    def test_inactive_products_are_also_resynced(self):
        self.item.is_active = False
        self.item.save()
        self._set_ingredient_allergens(self.flour, [self.gluten])
        self.assertEqual(self._allergens(self.item), {self.gluten})

    def test_renaming_an_ingredient_keeps_the_allergens(self):
        self._auth(self.admin)
        response = self.client.patch(f'/api/catalog/ingredients/{self.flour.id}/', {'name': 'Harina 2'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._allergens(self.item), {self.soy})

    # ---- end to end: the change reaches the POS and the preorders ------------
    def _sell(self, method, user, item=None):
        self._auth(self.staff)
        return self.client.post('/api/pos/sales/', {
            'payment_method': method, 'user_id': user.id,
            'items': [{'menu_item_id': (item or self.item).id, 'quantity': 1}],
        }, format='json')

    def test_pos_sale_is_blocked_after_an_allergen_is_added_to_an_ingredient(self):
        self.student.allergies.add(self.gluten)
        # Not blocked yet: the ingredient does not contain gluten.
        self.assertEqual(self._sell('CASH', self.student_user).status_code, 201)

        self._set_ingredient_allergens(self.flour, [self.soy, self.gluten])
        for method in ('CASH', 'WALLET'):
            response = self._sell(method, self.student_user)
            self.assertEqual(response.status_code, 400, method)
            self.assertIn('Gluten sync', str(response.data))
        self.assertEqual(Sale.objects.count(), 1)

    def test_pos_sale_is_allowed_again_after_the_allergen_is_removed(self):
        self.student.allergies.add(self.soy)
        self.assertEqual(self._sell('WALLET', self.student_user).status_code, 400)
        self._set_ingredient_allergens(self.flour, [])
        self.assertEqual(self._sell('WALLET', self.student_user).status_code, 201)

    def test_preorder_is_blocked_after_an_allergen_is_added_to_an_ingredient(self):
        delivery = local_today() + timedelta(days=2)
        monthly = MonthlyMenu.objects.create(
            institution=self.institution, year=delivery.year, month=delivery.month,
            status=MonthlyMenu.Status.PUBLISHED)
        DailyMenu.objects.create(monthly_menu=monthly, date=delivery).dishes.add(self.dish)
        self.student.allergies.add(self.gluten)

        self._set_ingredient_allergens(self.flour, [self.gluten])
        self._auth(self.parent_user)
        response = self.client.post('/api/pos/preorder/', {
            'student_id': self.student.id, 'delivery_date': str(delivery),
            'items': [{'dish_id': self.dish.id, 'quantity': 1}],
        }, format='json')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('Gluten sync', str(response.data))
        self.assertFalse(PreOrder.objects.exists())
