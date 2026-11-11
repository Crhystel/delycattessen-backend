from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from catalog.mixins import ViewerAllergenContextMixin
from catalog.models import Allergen, Combo, ComboItem, DailyMenu, Ingredient, MenuDish, MenuItem, MonthlyMenu, Promotion
from core.timezone import local_today
from users.models import CustomUser, Institution, ParentProfile, StudentProfile

GIF = (
    b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04'
    b'\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
)
MENU = '/api/catalog/menu/'
ACTIVE_PROMOTIONS = '/api/catalog/promotions/active/'
ACTIVE_COMBOS = '/api/catalog/combos/active/'


def _image(name):
    return SimpleUploadedFile(name, GIF, content_type='image/gif')


class ViewerAllergenTests(APITestCase):
    """The server marks the products that conflict with the allergies of whoever
    is asking (student, teacher, or the parent's own child), and nobody else."""

    def setUp(self):
        make = CustomUser.objects.create_user
        self.institution = Institution.objects.create(name='Sede A')
        self.gluten = Allergen.objects.create(name='Gluten viewer')
        self.soy = Allergen.objects.create(name='Soya viewer')
        self.milk = Allergen.objects.create(name='Leche viewer')

        self.parent_user = make(username='parent', email='p@x.com', password='x', role=CustomUser.Role.PARENT)
        parent = ParentProfile.objects.create(user=self.parent_user)
        self.student_user = make(username='student', email='s@x.com', password='x',
                                 role=CustomUser.Role.STUDENT, institution=self.institution)
        self.student = StudentProfile.objects.create(
            user=self.student_user, parent=parent, institution=self.institution, profile_picture=_image('a.gif'))
        self.student.allergies.add(self.gluten)

        other_parent_user = make(username='parent2', email='p2@x.com', password='x', role=CustomUser.Role.PARENT)
        other_parent = ParentProfile.objects.create(user=other_parent_user)
        stranger_user = make(username='stranger', email='s2@x.com', password='x',
                             role=CustomUser.Role.STUDENT, institution=self.institution)
        self.stranger = StudentProfile.objects.create(
            user=stranger_user, parent=other_parent, institution=self.institution, profile_picture=_image('b.gif'))
        self.stranger.allergies.add(self.soy)

        self.teacher = make(username='teacher', email='t@x.com', password='x',
                            role=CustomUser.Role.TEACHER, institution=self.institution)
        self.teacher.allergies.add(self.soy, self.milk)
        self.staff = make(username='staff', email='st@x.com', password='x',
                          role=CustomUser.Role.OPERATIONS_STAFF, institution=self.institution)
        self.admin = make(username='admin', email='ad@x.com', password='x',
                          role=CustomUser.Role.ADMIN, institution=self.institution)

        self.ingredient = Ingredient.objects.create(name='Base viewer', allergens_reviewed=True)
        self.bread = self._item('Pan', [self.gluten, self.soy])
        self.juice = self._item('Jugo', [])

    def _item(self, name, allergens):
        item = MenuItem.objects.create(
            institution=self.institution, name=name, price=Decimal('2.00'), stock=10, image=_image(f'{name}.gif'))
        item.ingredients.add(self.ingredient)
        item.allergens.set(allergens)
        return item

    def _auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    @staticmethod
    def _by_name(rows):
        return {row['name']: row for row in rows}

    # ---- menu (MenuItemSerializer) ------------------------------------------
    def test_student_gets_own_conflicts_on_the_menu(self):
        self._auth(self.student_user)
        rows = self._by_name(self.client.get(MENU).data)
        self.assertEqual(rows['Pan']['conflicting_allergens'], ['Gluten viewer'])
        self.assertTrue(rows['Pan']['has_viewer_allergens'])
        self.assertEqual(rows['Jugo']['conflicting_allergens'], [])
        self.assertFalse(rows['Jugo']['has_viewer_allergens'])

    def test_teacher_gets_own_conflicts_on_the_menu(self):
        self._auth(self.teacher)
        rows = self._by_name(self.client.get(MENU).data)
        self.assertEqual(rows['Pan']['conflicting_allergens'], ['Soya viewer'])
        self.assertTrue(rows['Pan']['has_viewer_allergens'])

    def test_parent_gets_the_conflicts_of_their_own_child(self):
        self._auth(self.parent_user)
        response = self.client.get(MENU, {'student_id': self.student.id})
        self.assertEqual(response.status_code, 200)
        rows = self._by_name(response.data)
        self.assertEqual(rows['Pan']['conflicting_allergens'], ['Gluten viewer'])
        self.assertTrue(rows['Pan']['has_viewer_allergens'])

    def test_parent_never_sees_the_conflicts_of_someone_elses_child(self):
        self._auth(self.parent_user)
        response = self.client.get(MENU, {'student_id': self.stranger.id})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('Soya viewer', str(response.data))

    def test_context_is_empty_for_a_parent_asking_for_a_stranger(self):
        view = ViewerAllergenContextMixin()
        view.request = SimpleNamespace(
            user=self.parent_user, query_params={'student_id': str(self.stranger.id)})
        self.assertEqual(view.get_viewer_allergy_ids(), set())
        view.request.query_params = {'student_id': 'abc'}
        self.assertEqual(view.get_viewer_allergy_ids(), set())
        view.request.query_params = {}
        self.assertEqual(view.get_viewer_allergy_ids(), set())

    def test_staff_and_admin_get_no_marks(self):
        for user in (self.staff, self.admin):
            self._auth(user)
            for row in self.client.get(MENU).data:
                self.assertEqual(row['conflicting_allergens'], [], user.role)
                self.assertFalse(row['has_viewer_allergens'], user.role)

    def test_existing_menu_fields_are_unchanged(self):
        self._auth(self.student_user)
        row = self._by_name(self.client.get(MENU).data)['Pan']
        self.assertEqual(
            {k: row[k] for k in ('id', 'name', 'price', 'stock', 'discount_percentage', 'final_price')},
            {'id': self.bread.id, 'name': 'Pan', 'price': '2.00', 'stock': 10,
             'discount_percentage': None, 'final_price': '2.00'},
        )
        self.assertEqual({a['name'] for a in row['allergens']}, {'Gluten viewer', 'Soya viewer'})
        self.assertEqual(set(row['allergens'][0]), {'id', 'name', 'description'})
        self.assertEqual([i['name'] for i in row['ingredients']], ['Base viewer'])

    # ---- promotions ----------------------------------------------------------
    def _promotion(self):
        today = local_today()
        promotion = Promotion.objects.create(
            institution=self.institution, discount_percentage=20,
            start_date=today, end_date=today + timedelta(days=5))
        promotion.products.set([self.bread, self.juice])
        return promotion

    def test_active_promotions_mark_the_viewer_conflicts(self):
        self._promotion()
        self._auth(self.student_user)
        response = self.client.get(ACTIVE_PROMOTIONS)
        self.assertEqual(response.status_code, 200)
        products = {p['name']: p for p in response.data[0]['products']}
        self.assertEqual(products['Pan']['conflicting_allergens'], ['Gluten viewer'])
        self.assertTrue(products['Pan']['has_viewer_allergens'])
        self.assertEqual({a['name'] for a in products['Pan']['allergens']}, {'Gluten viewer', 'Soya viewer'})
        self.assertEqual(products['Jugo']['conflicting_allergens'], [])
        self.assertEqual(response.data[0]['discount_percentage'], 20)

    def test_active_promotions_are_unmarked_for_staff_and_for_a_strangers_child(self):
        self._promotion()
        self._auth(self.staff)
        for product in self.client.get(ACTIVE_PROMOTIONS).data[0]['products']:
            self.assertEqual(product['conflicting_allergens'], [])
            self.assertFalse(product['has_viewer_allergens'])
        self._auth(self.parent_user)
        self.assertEqual(self.client.get(ACTIVE_PROMOTIONS, {'student_id': self.stranger.id}).status_code, 403)

    # ---- combos --------------------------------------------------------------
    def _combo(self):
        today = local_today()
        combo = Combo.objects.create(
            institution=self.institution, name='Combo viewer', price=Decimal('3.00'),
            start_date=today, end_date=today + timedelta(days=5), image=_image('c.gif'))
        ComboItem.objects.create(combo=combo, menu_item=self.bread, quantity=1)
        ComboItem.objects.create(combo=combo, menu_item=self.juice, quantity=2)
        return combo

    def test_active_combos_mark_the_viewer_conflicts_per_item(self):
        self._combo()
        self._auth(self.teacher)
        response = self.client.get(ACTIVE_COMBOS)
        self.assertEqual(response.status_code, 200)
        items = self._by_name(response.data[0]['items'])
        self.assertEqual(items['Pan']['conflicting_allergens'], ['Soya viewer'])
        self.assertTrue(items['Pan']['has_viewer_allergens'])
        self.assertEqual({a['name'] for a in items['Pan']['allergens']}, {'Gluten viewer', 'Soya viewer'})
        self.assertEqual(items['Jugo']['conflicting_allergens'], [])
        self.assertEqual(items['Jugo']['allergens'], [])
        # Existing combo item fields stay.
        self.assertEqual(items['Jugo']['quantity'], 2)
        self.assertEqual(items['Jugo']['price'], '2.00')

    def test_active_combos_are_unmarked_for_staff(self):
        self._combo()
        self._auth(self.staff)
        for item in self.client.get(ACTIVE_COMBOS).data[0]['items']:
            self.assertEqual(item['conflicting_allergens'], [])
            self.assertFalse(item['has_viewer_allergens'])

    def test_admin_combo_list_does_not_mark_anything(self):
        self._combo()
        self._auth(self.admin)
        response = self.client.get('/api/catalog/combos/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual({tuple(i['conflicting_allergens']) for i in response.data[0]['items']}, {()})

    # ---- monthly menu dishes ----------------------------------------------------
    def test_student_dishes_list_the_conflicting_allergens(self):
        delivery = local_today() + timedelta(days=2)
        monthly = MonthlyMenu.objects.create(
            institution=self.institution, year=delivery.year, month=delivery.month,
            status=MonthlyMenu.Status.PUBLISHED)
        dish = MenuDish.objects.create(
            institution=self.institution, name='Pasta viewer', price=Decimal('3.00'), image=_image('d.gif'))
        dish.allergens.add(self.gluten, self.soy)
        DailyMenu.objects.create(monthly_menu=monthly, date=delivery).dishes.add(dish)

        self._auth(self.parent_user)
        response = self.client.get(f'/api/catalog/students/{self.student.id}/monthly_menu/')
        self.assertEqual(response.status_code, 200)
        row = next(d for day in response.data for d in day['dishes'] if d['id'] == dish.id)
        self.assertEqual(row['conflicting_allergens'], ['Gluten viewer'])
        # Existing fields are untouched.
        self.assertTrue(row['has_student_allergens'])
        self.assertCountEqual(row['allergens'], ['Gluten viewer', 'Soya viewer'])
