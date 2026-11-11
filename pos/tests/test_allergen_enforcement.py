from datetime import timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from catalog.models import Allergen, Combo, ComboItem, DailyMenu, Ingredient, MenuDish, MenuItem, MonthlyMenu
from core.timezone import local_today
from pos.models import PreOrder, Sale
from users.models import CustomUser, Institution, ParentProfile, StudentProfile
from wallet.models import Transaction, Wallet

GIF = (
    b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04'
    b'\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
)
SALES = '/api/pos/sales/'
PREORDER = '/api/pos/preorder/'


def _image(name):
    return SimpleUploadedFile(name, GIF, content_type='image/gif')


class AllergenEnforcementTests(APITestCase):
    """The allergen block has no bypass: not by payment method, not by who
    operates the sale (cashier or admin), and the message fits the allergic person."""

    def setUp(self):
        make = CustomUser.objects.create_user
        self.institution = Institution.objects.create(name='Sede A')
        self.staff = make(username='staff', email='sa@x.com', password='x',
                          role=CustomUser.Role.OPERATIONS_STAFF, institution=self.institution)
        self.admin = make(username='admin', email='aa@x.com', password='x',
                          role=CustomUser.Role.ADMIN, institution=self.institution)
        self.teacher = make(username='teacher', email='ta@x.com', password='x',
                            role=CustomUser.Role.TEACHER, institution=self.institution)
        self.parent_user = make(username='parent', email='p@x.com', password='x', role=CustomUser.Role.PARENT)
        parent = ParentProfile.objects.create(user=self.parent_user)
        self.student_user = make(username='student', email='st@x.com', password='x',
                                 role=CustomUser.Role.STUDENT, institution=self.institution)
        self.student = StudentProfile.objects.create(
            user=self.student_user, parent=parent, institution=self.institution, profile_picture=_image('s.gif'))
        self.wallet = Wallet.objects.create(
            student=self.student, balance=Decimal('50.00'), low_balance_threshold=Decimal('1.00'))

        self.gluten = Allergen.objects.create(name='Gluten enforce')
        self.student.allergies.add(self.gluten)
        self.teacher.allergies.add(self.gluten)

        ingredient = Ingredient.objects.create(name='Harina enforce', allergens_reviewed=True)
        ingredient.allergens.add(self.gluten)
        self.item = MenuItem.objects.create(
            institution=self.institution, name='Pan enforce', price=Decimal('1.50'), stock=10)
        self.item.ingredients.add(ingredient)
        self.item.allergens.add(self.gluten)

        today = local_today()
        self.combo = Combo.objects.create(
            institution=self.institution, name='Combo enforce', price=Decimal('3.00'),
            start_date=today, end_date=today + timedelta(days=5), image=_image('c.gif'))
        ComboItem.objects.create(combo=self.combo, menu_item=self.item, quantity=1)

    def _auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _sell(self, method, customer, combos=False):
        payload = {'payment_method': method, 'user_id': customer.id, 'items': [], 'combos': []}
        if combos:
            payload['combos'] = [{'combo_id': self.combo.id, 'quantity': 1}]
        else:
            payload['items'] = [{'menu_item_id': self.item.id, 'quantity': 1}]
        return self.client.post(SALES, payload, format='json')

    def _assert_nothing_sold(self):
        self.item.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(self.item.stock, 10)
        self.assertEqual(self.wallet.balance, Decimal('50.00'))
        self.assertFalse(Sale.objects.exists())
        self.assertFalse(Transaction.objects.exists())

    # ---- message by owner ------------------------------------------------------
    def test_message_for_a_student_names_the_student(self):
        self._auth(self.staff)
        response = self._sell('CASH', self.student_user)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data,
            ['El producto "Pan enforce" contiene alérgenos que afectan al estudiante: Gluten enforce'],
        )

    def test_message_for_a_teacher_speaks_to_the_teacher(self):
        self._auth(self.staff)
        response = self._sell('CREDIT', self.teacher)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data,
            ['El producto "Pan enforce" contiene alérgenos que te afectan: Gluten enforce'],
        )
        self.assertNotIn('estudiante', str(response.data))

    # ---- robustness ------------------------------------------------------------
    def test_wallet_sale_to_a_student_account_without_profile_is_a_400(self):
        orphan = CustomUser.objects.create_user(
            username='orphan', email='o@x.com', password='x',
            role=CustomUser.Role.STUDENT, institution=self.institution)
        self._auth(self.staff)
        response = self._sell('WALLET', orphan)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(str(response.data['user_id']), 'El cliente no tiene un perfil de estudiante.')
        self._assert_nothing_sold()

    # ---- no bypass ---------------------------------------------------------------
    def test_no_payment_method_nor_operator_can_skip_the_restriction(self):
        cases = (
            ('CASH', self.student_user), ('WALLET', self.student_user),
            ('CASH', self.teacher), ('CREDIT', self.teacher),
        )
        for operator in (self.staff, self.admin):
            self._auth(operator)
            for method, customer in cases:
                for combos in (False, True):
                    response = self._sell(method, customer, combos=combos)
                    self.assertEqual(
                        response.status_code, 400, (operator.role, method, customer.role, combos, response.data))
        self._assert_nothing_sold()

    def test_non_allergic_customers_can_still_buy_the_same_product(self):
        self.student.allergies.clear()
        self._auth(self.admin)
        self.assertEqual(self._sell('WALLET', self.student_user).status_code, 201)

    def test_preorder_is_blocked_for_the_parent_and_unavailable_to_cashier_and_admin(self):
        delivery = local_today() + timedelta(days=2)
        monthly = MonthlyMenu.objects.create(
            institution=self.institution, year=delivery.year, month=delivery.month,
            status=MonthlyMenu.Status.PUBLISHED)
        dish = MenuDish.objects.create(
            institution=self.institution, name='Pasta enforce', price=Decimal('3.00'), image=_image('d.gif'))
        dish.allergens.add(self.gluten)
        DailyMenu.objects.create(monthly_menu=monthly, date=delivery).dishes.add(dish)
        payload = {
            'student_id': self.student.id, 'delivery_date': str(delivery),
            'items': [{'dish_id': dish.id, 'quantity': 1}],
        }

        self._auth(self.parent_user)
        response = self.client.post(PREORDER, payload, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('afectan al estudiante: Gluten enforce', str(response.data))

        for operator in (self.staff, self.admin):
            self._auth(operator)
            self.assertEqual(self.client.post(PREORDER, payload, format='json').status_code, 403)
        self.assertFalse(PreOrder.objects.exists())
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('50.00'))
