"""Database rules that keep history intact: nothing financial is deleted by cascade and
the invariants the API assumes are enforced by the database itself."""
from datetime import date, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase

from catalog.models import DailyMenu, MenuItem, MonthlyMenu, Promotion
from pos.models import PreOrder
from users.models import CustomUser, Institution, ParentProfile, StudentProfile
from wallet.models import Transaction, Wallet

Role = CustomUser.Role


class DatabaseIntegrityTests(TestCase):
    def setUp(self):
        self.institution = Institution.objects.create(name='Martim Cereré')
        self.parent_user = CustomUser.objects.create_user(
            username='padre', email='padre@x.com', password='x', role=Role.PARENT,
        )
        self.parent = ParentProfile.objects.create(user=self.parent_user)
        self.student_user = CustomUser.objects.create_user(
            username='hijo', email='hijo@x.com', password='x', role=Role.STUDENT,
        )
        self.student = StudentProfile.objects.create(
            user=self.student_user, institution=self.institution, parent=self.parent,
        )
        self.wallet = Wallet.objects.create(student=self.student, balance=Decimal('5.00'), low_balance_threshold=2)

    def assert_rejected(self, create):
        with self.assertRaises(IntegrityError), transaction.atomic():
            create()

    # ------------------------------------------------------- no cascade deletes
    def test_wallet_with_history_cannot_be_deleted(self):
        Transaction.objects.create(
            wallet=self.wallet, amount=Decimal('5.00'), type=Transaction.Type.RECHARGE,
            status=Transaction.Status.SUCCESS, gateway=Transaction.Gateway.KUSHKI, external_transaction_id='T-1',
        )
        with self.assertRaises(ProtectedError):
            self.wallet.delete()

    def test_student_profile_with_a_wallet_cannot_be_deleted(self):
        with self.assertRaises(ProtectedError):
            self.student.delete()

    def test_accounts_with_profiles_cannot_be_deleted(self):
        for user in (self.parent_user, self.student_user):
            with self.assertRaises(ProtectedError):
                user.delete()

    def test_parent_with_children_cannot_be_deleted(self):
        with self.assertRaises(ProtectedError):
            self.parent.delete()

    def test_student_with_preorders_cannot_be_deleted(self):
        menu = MonthlyMenu.objects.create(institution=self.institution, year=2026, month=11)
        day = DailyMenu.objects.create(monthly_menu=menu, date=date(2026, 11, 20))
        PreOrder.objects.create(student=self.student, daily_menu=day, total_amount=Decimal('3.00'))
        with self.assertRaises(ProtectedError):
            self.student.delete()

    # ------------------------------------------------------------ accounts
    def test_email_is_unique_ignoring_case(self):
        self.assert_rejected(lambda: CustomUser.objects.create_user(username='otro', email='PADRE@X.COM', password='x'))

    def test_several_accounts_may_have_no_email(self):
        # Soft-deleted accounts keep email=None; NULLs never collide.
        CustomUser.objects.create(username='a', email=None, role=Role.STUDENT)
        CustomUser.objects.create(username='b', email=None, role=Role.STUDENT)

    def test_role_must_be_a_known_one(self):
        self.assert_rejected(lambda: CustomUser.objects.create_user(username='x', password='x', role='HACKER'))

    def test_a_soft_deleted_account_cannot_be_active(self):
        from django.utils import timezone
        self.parent_user.deleted_at = timezone.now()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.parent_user.save()

    # -------------------------------------------------------------- money
    def test_balance_cannot_be_negative(self):
        self.wallet.balance = Decimal('-0.01')
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.wallet.save()

    def test_transaction_amount_must_be_positive(self):
        self.assert_rejected(lambda: Transaction.objects.create(
            wallet=self.wallet, amount=Decimal('0.00'), type=Transaction.Type.RECHARGE,
        ))

    def test_gateway_reference_is_unique_but_blank_ones_repeat(self):
        make = lambda gateway, ref: Transaction.objects.create(  # noqa: E731
            wallet=self.wallet, amount=Decimal('1.00'), type=Transaction.Type.RECHARGE,
            gateway=gateway, external_transaction_id=ref,
        )
        make(Transaction.Gateway.KUSHKI, 'REF-1')
        make(Transaction.Gateway.PAYPHONE, 'REF-1')  # another gateway: allowed
        make('', '')
        make('', '')  # consumption-like rows have no reference: allowed
        self.assert_rejected(lambda: make(Transaction.Gateway.KUSHKI, 'REF-1'))

    # ------------------------------------------------------------ catalog
    def test_product_needs_a_valid_price_and_stock(self):
        for fields in ({'price': Decimal('0'), 'stock': 1}, {'price': Decimal('1'), 'stock': -1}):
            self.assert_rejected(lambda: MenuItem.objects.create(
                institution=self.institution, name='x', category='c', **fields,
            ))

    def test_offer_cannot_end_before_it_starts(self):
        today = date.today()
        self.assert_rejected(lambda: Promotion.objects.create(
            institution=self.institution, discount_percentage=10,
            start_date=today, end_date=today - timedelta(days=1),
        ))
