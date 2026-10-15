from datetime import datetime, timedelta
from decimal import Decimal
import zoneinfo

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from catalog.models import Allergen, Ingredient, MenuItem
from pos.models import Sale
from users.models import CustomUser, Institution, ParentalControl, ParentProfile, StudentProfile
from wallet.models import Transaction, Wallet

GIF = (
    b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04'
    b'\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
)
SALES = '/api/pos/sales/'


class SaleTests(APITestCase):
    def setUp(self):
        make = CustomUser.objects.create_user
        self.inst_a = Institution.objects.create(name='Sede A')
        self.inst_b = Institution.objects.create(name='Sede B')
        self.staff_a = make(username='staff_a', email='sa@x.com', password='x',
                            role=CustomUser.Role.OPERATIONS_STAFF, institution=self.inst_a)
        self.admin_a = make(username='admin_a', email='aa@x.com', password='x',
                            role=CustomUser.Role.ADMIN, institution=self.inst_a)
        self.teacher_a = make(username='teacher_a', email='ta@x.com', password='x',
                              role=CustomUser.Role.TEACHER, institution=self.inst_a)
        self.teacher_b = make(username='teacher_b', email='tb@x.com', password='x',
                              role=CustomUser.Role.TEACHER, institution=self.inst_b)
        self.parent_user = make(username='parent', email='p@x.com', password='x', role=CustomUser.Role.PARENT)
        parent = ParentProfile.objects.create(user=self.parent_user)
        self.student_user = make(username='student', email='st@x.com', password='x', role=CustomUser.Role.STUDENT)
        self.student = StudentProfile.objects.create(
            user=self.student_user, parent=parent, institution=self.inst_a,
            profile_picture=SimpleUploadedFile('s.gif', GIF, content_type='image/gif'))
        self.wallet = Wallet.objects.create(
            student=self.student, balance=Decimal('10.00'), low_balance_threshold=Decimal('1.00'))

        ingredient = Ingredient.objects.create(name='Base', allergens_reviewed=True)
        self.item = self._item(self.inst_a, 'Sandwich', '2.50', stock=5, ingredient=ingredient)
        self.other_item = self._item(self.inst_a, 'Jugo', '1.00', stock=3, ingredient=ingredient)
        self.item_b = self._item(self.inst_b, 'Pan B', '1.00', stock=3, ingredient=ingredient)

    def _item(self, institution, name, price, stock, ingredient):
        item = MenuItem.objects.create(
            institution=institution, name=name, price=Decimal(price), stock=stock)
        item.ingredients.add(ingredient)
        return item

    def _auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _sell(self, method, items=None, user=None, query=''):
        payload = {
            'payment_method': method,
            'items': items if items is not None else [{'menu_item_id': self.item.id, 'quantity': 2}],
        }
        if user is not None:
            payload['user_id'] = user.id
        return self.client.post(SALES + query, payload, format='json')

    def _stock(self, item):
        item.refresh_from_db()
        return item.stock

    # ---- quick sale / cash ------------------------------------------------
    def test_quick_sale_is_cash_without_customer_and_discounts_stock(self):
        self._auth(self.staff_a)
        response = self._sell('CASH')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['total_amount'], '5.00')
        sale = Sale.objects.get()
        self.assertIsNone(sale.customer)
        self.assertEqual((sale.cashier, sale.institution), (self.staff_a, self.inst_a))
        self.assertEqual(self._stock(self.item), 3)

    def test_product_is_hidden_when_stock_reaches_zero(self):
        self._auth(self.staff_a)
        self.assertEqual(self._sell('CASH', [{'menu_item_id': self.other_item.id, 'quantity': 3}]).status_code, 201)
        self.other_item.refresh_from_db()
        self.assertEqual((self.other_item.stock, self.other_item.is_visible), (0, False))

    def test_credit_and_wallet_require_a_customer(self):
        self._auth(self.staff_a)
        for method in ('CREDIT', 'WALLET'):
            self.assertEqual(self._sell(method).status_code, 400)
        self.assertFalse(Sale.objects.exists())

    def test_duplicated_lines_are_merged(self):
        self._auth(self.staff_a)
        lines = [{'menu_item_id': self.item.id, 'quantity': 2}, {'menu_item_id': self.item.id, 'quantity': 1}]
        self.assertEqual(self._sell('CASH', lines).status_code, 201)
        self.assertEqual(self._stock(self.item), 2)

    # ---- teacher ------------------------------------------------------------
    def test_teacher_pays_on_credit_and_cash(self):
        self._auth(self.staff_a)
        self.assertEqual(self._sell('CREDIT', user=self.teacher_a).status_code, 201)
        self.assertEqual(self._sell('CASH', user=self.teacher_a).status_code, 201)
        self.assertEqual(Sale.objects.filter(customer=self.teacher_a).count(), 2)
        self.assertEqual(self._stock(self.item), 1)

    def test_only_teachers_can_use_credit(self):
        self._auth(self.staff_a)
        for customer in (self.student_user, self.parent_user, self.staff_a):
            self.assertEqual(self._sell('CREDIT', user=customer).status_code, 403)
        self.assertFalse(Sale.objects.exists())

    def test_teacher_of_another_institution_is_rejected(self):
        self._auth(self.staff_a)
        self.assertEqual(self._sell('CREDIT', user=self.teacher_b).status_code, 400)

    def test_teacher_allergen_blocks_credit_and_cash(self):
        allergen = Allergen.objects.create(name='Gluten test')
        self.item.allergens.add(allergen)
        self.teacher_a.allergies.add(allergen)
        self._auth(self.staff_a)
        for method in ('CREDIT', 'CASH'):
            self.assertEqual(self._sell(method, user=self.teacher_a).status_code, 400)
        self.assertEqual(self._stock(self.item), 5)

    # ---- student ------------------------------------------------------------
    def test_student_pays_with_wallet(self):
        self._auth(self.staff_a)
        self.assertEqual(self._sell('WALLET', user=self.student_user).status_code, 201)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('5.00'))
        transaction = Transaction.objects.get(wallet=self.wallet)
        self.assertEqual((transaction.type, transaction.status, transaction.amount),
                         (Transaction.Type.CONSUMPTION, Transaction.Status.SUCCESS, Decimal('5.00')))

    def test_wallet_with_insufficient_balance_changes_nothing(self):
        self.wallet.balance = Decimal('1.00')
        self.wallet.save()
        self._auth(self.staff_a)
        self.assertEqual(self._sell('WALLET', user=self.student_user).status_code, 400)
        self.assertEqual(self._stock(self.item), 5)
        self.assertFalse(Sale.objects.exists())
        self.assertFalse(Transaction.objects.exists())

    def test_only_students_can_use_wallet(self):
        self._auth(self.staff_a)
        self.assertEqual(self._sell('WALLET', user=self.teacher_a).status_code, 403)

    def test_student_allergen_blocks_wallet_and_cash(self):
        allergen = Allergen.objects.create(name='Lactosa test')
        self.item.allergens.add(allergen)
        self.student.allergies.add(allergen)
        self._auth(self.staff_a)
        for method in ('WALLET', 'CASH'):
            self.assertEqual(self._sell(method, user=self.student_user).status_code, 400)
        self.assertEqual(self.wallet.balance, Decimal('10.00'))

    def test_parental_controls_apply_to_wallet_only(self):
        today = datetime.now(zoneinfo.ZoneInfo('America/Guayaquil')).date()
        ParentalControl.objects.create(
            student=self.student, allowed_days_enabled=True, allowed_days=[(today.weekday() + 1) % 7])
        self._auth(self.staff_a)
        self.assertEqual(self._sell('WALLET', user=self.student_user).status_code, 400)
        self.assertEqual(self._sell('CASH', user=self.student_user).status_code, 201)

    def test_wallet_purchase_counts_toward_daily_limit(self):
        ParentalControl.objects.create(
            student=self.student, daily_limit_enabled=True, daily_limit_amount=Decimal('6.00'))
        self._auth(self.staff_a)
        self.assertEqual(self._sell('WALLET', user=self.student_user).status_code, 201)
        self.assertEqual(self._sell('WALLET', user=self.student_user).status_code, 400)

    # ---- stock / institution --------------------------------------------------
    def test_insufficient_stock_rejects_whole_sale(self):
        self._auth(self.staff_a)
        lines = [{'menu_item_id': self.other_item.id, 'quantity': 1}, {'menu_item_id': self.item.id, 'quantity': 6}]
        self.assertEqual(self._sell('CASH', lines).status_code, 400)
        self.assertEqual((self._stock(self.item), self._stock(self.other_item)), (5, 3))
        self.assertFalse(Sale.objects.exists())

    def test_product_of_another_institution_is_rejected(self):
        self._auth(self.staff_a)
        self.assertEqual(self._sell('CASH', [{'menu_item_id': self.item_b.id, 'quantity': 1}]).status_code, 400)

    def test_admin_sells_in_targeted_institution(self):
        self._auth(self.admin_a)
        own = self._sell('CASH', [{'menu_item_id': self.item_b.id, 'quantity': 1}])
        self.assertEqual(own.status_code, 400)
        other = self._sell('CASH', [{'menu_item_id': self.item_b.id, 'quantity': 1}],
                           query=f'?institution={self.inst_b.id}')
        self.assertEqual(other.status_code, 201, other.data)
        self.assertEqual(Sale.objects.get().institution, self.inst_b)

    def test_non_operators_cannot_sell(self):
        for user in (self.parent_user, self.teacher_a, self.student_user):
            self._auth(user)
            self.assertEqual(self._sell('CASH').status_code, 403)

    # ---- credit history --------------------------------------------------------
    def test_credit_history_shows_own_credit_sales_and_month_total(self):
        self._auth(self.staff_a)
        self._sell('CREDIT', user=self.teacher_a)
        self._sell('CASH', user=self.teacher_a)
        self.assertEqual(self._sell('CREDIT', user=self.teacher_a,
                                    items=[{'menu_item_id': self.other_item.id, 'quantity': 1}]).status_code, 201)
        old = Sale.objects.create(
            institution=self.inst_a, cashier=self.staff_a, customer=self.teacher_a,
            payment_method=Sale.PaymentMethod.CREDIT, total_amount=Decimal('9.00'))
        Sale.objects.filter(pk=old.pk).update(created_at=datetime.now(zoneinfo.ZoneInfo('UTC')) - timedelta(days=62))

        self._auth(self.teacher_a)
        response = self.client.get('/api/pos/credit/history/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['accumulated_total'], '6.00')
        self.assertEqual(len(response.data['results']), 3)
        self.assertEqual({row['payment_method'] for row in response.data['results']}, {'CREDIT'})

        self._auth(self.teacher_b)
        self.assertEqual(self.client.get('/api/pos/credit/history/').data['results'], [])

    def test_credit_history_is_teacher_only(self):
        for user in (self.staff_a, self.parent_user, self.student_user):
            self._auth(user)
            self.assertEqual(self.client.get('/api/pos/credit/history/').status_code, 403)
