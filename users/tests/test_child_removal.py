import io
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APITestCase
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken

from catalog.models import Allergen, DailyMenu, MonthlyMenu
from core.timezone import local_today
from pos.models import PreOrder
from users.models import (
    AuditLog, CustomUser, Institution, ParentalControl, ParentProfile, StudentProfile, UserBiometric,
)
from users.serializers import StudentRegistrationSerializer
from wallet.models import Transaction, Wallet
from wallet.permissions import IsWalletOwnerParent

Role = CustomUser.Role
LOCMEM_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
REFRESH_URL = '/api/users/login/refresh/'
PARENT_PASSWORD = 'Parent-Passw0rd!'
CHILD_PASSWORD = 'Child-Passw0rd!'


def make_family(institution, suffix, parent_email=None):
    parent_user = CustomUser.objects.create_user(
        username=f'padre{suffix}', email=parent_email or f'padre{suffix}@x.com', password=PARENT_PASSWORD,
        role=Role.PARENT, first_name='ANA', last_name='PEREZ',
    )
    parent = ParentProfile.objects.create(user=parent_user)
    return parent_user, parent


def add_child(parent, institution, suffix, balance='0.00'):
    user = CustomUser.objects.create_user(
        username=f'hijo{suffix}', email=f'hijo{suffix}@x.com', password=CHILD_PASSWORD, role=Role.STUDENT,
        first_name='LUIS', last_name='PEREZ',
    )
    student = StudentProfile.objects.create(user=user, institution=institution, parent=parent)
    Wallet.objects.create(student=student, balance=Decimal(balance), low_balance_threshold=Decimal('2.00'))
    return student


def make_pre_order(student, day, status=PreOrder.Status.PENDING):
    monthly, _created = MonthlyMenu.objects.get_or_create(
        institution=student.institution, year=day.year, month=day.month,
        defaults={'status': MonthlyMenu.Status.PUBLISHED},
    )
    daily, _created = DailyMenu.objects.get_or_create(monthly_menu=monthly, date=day)
    return PreOrder.objects.create(student=student, daily_menu=daily, total_amount=Decimal('2.00'), status=status)


def make_recharge(wallet, status=Transaction.Status.PENDING, minutes_ago=0):
    transaction = Transaction.objects.create(
        wallet=wallet, amount=Decimal('5.00'), type=Transaction.Type.RECHARGE,
        gateway=Transaction.Gateway.KUSHKI, status=status,
    )
    Transaction.objects.filter(pk=transaction.pk).update(created_at=timezone.now() - timedelta(minutes=minutes_ago))
    return transaction


def png_upload():
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8), 'white').save(buffer, format='PNG')
    return SimpleUploadedFile('face.png', buffer.getvalue(), content_type='image/png')


@override_settings(CACHES=LOCMEM_CACHE)
class ChildRemovalTests(APITestCase):
    """The parent removes a child confirming with their password. It is a soft delete
    and it is blocked while the child has a balance, open pre-orders or a recharge
    in process."""

    def setUp(self):
        cache.clear()
        self.institution = Institution.objects.create(name='Sede A')
        self.parent_user, self.parent = make_family(self.institution, 'a')
        self.student = add_child(self.parent, self.institution, 'a')
        self.child = self.student.user
        self.child_session = RefreshToken.for_user(self.child)
        self.parent_session = RefreshToken.for_user(self.parent_user)
        self.authenticate(self.parent_session)

    def authenticate(self, session):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {session.access_token}')

    def url(self, action, student=None):
        return f'/api/users/children/{(student or self.student).pk}/{action}/'

    def remove(self, password=PARENT_PASSWORD, student=None):
        return self.client.post(self.url('remove', student), {'parent_password': password}, format='json')

    def check(self, student=None):
        return self.client.get(self.url('removal-check', student))

    # --- successful removal -------------------------------------------------

    def test_the_removal_is_a_soft_delete_that_frees_the_account(self):
        response = self.remove()
        self.assertEqual(response.status_code, 200)
        self.child.refresh_from_db()
        self.assertFalse(self.child.is_active)
        self.assertIsNotNone(self.child.deleted_at)
        self.assertEqual(self.child.username, f'deleted-{self.child.pk}')
        self.assertIsNone(self.child.email)
        self.assertFalse(self.child.has_usable_password())
        self.assertTrue(StudentProfile.objects.filter(pk=self.student.pk).exists())
        self.assertTrue(Wallet.objects.filter(student=self.student).exists())

    def test_the_personal_data_of_the_minor_is_erased(self):
        allergen, _created = Allergen.objects.get_or_create(name='Maní')
        self.student.allergies.add(allergen)
        ParentalControl.objects.create(student=self.student)
        UserBiometric.objects.create(
            user=self.child, encrypted_embedding=b'x', nonce=b'n' * 12, tag=b't' * 16,
        )
        name = default_storage.save('profiles/students/face.png', ContentFile(b'photo'))
        StudentProfile.objects.filter(pk=self.student.pk).update(profile_picture=name)

        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.remove().status_code, 200)

        self.student.refresh_from_db()
        self.assertEqual(self.student.allergies.count(), 0)
        self.assertFalse(ParentalControl.objects.filter(student=self.student).exists())
        self.assertFalse(UserBiometric.objects.filter(user=self.child).exists())
        self.assertFalse(self.student.profile_picture)
        self.assertFalse(default_storage.exists(name))

    def test_the_child_sessions_close_but_the_parent_keeps_theirs(self):
        self.remove()
        self.assertEqual(self.client.post(REFRESH_URL, {'refresh': str(self.child_session)}).status_code, 401)
        self.assertEqual(self.client.post(REFRESH_URL, {'refresh': str(self.parent_session)}).status_code, 200)

    def test_the_removal_is_audited(self):
        self.remove()
        entry = AuditLog.objects.get(action=AuditLog.Action.ACCOUNT_REMOVED)
        self.assertEqual(entry.actor_id, self.parent_user.id)
        self.assertEqual(entry.target_id, self.child.id)

    def test_the_removed_child_disappears_for_the_parent(self):
        other = add_child(self.parent, self.institution, 'b')
        self.remove()

        ids = [child['id'] for child in self.client.get('/api/users/children/').json()]
        self.assertEqual(ids, [other.pk])
        self.assertTrue(self.client.get('/api/users/me/').json()['has_children'])

        self.remove(student=other)
        self.assertEqual(self.client.get('/api/users/children/').json(), [])
        self.assertFalse(self.client.get('/api/users/me/').json()['has_children'])

    def test_the_removed_child_cannot_be_reached_any_more(self):
        self.remove()
        for method, action in (('get', 'removal-check'), ('post', 'remove'), ('post', 'password')):
            response = getattr(self.client, method)(self.url(action))
            self.assertEqual(response.status_code, 403, action)
        self.assertEqual(self.client.patch(f'/api/users/children/{self.student.pk}/', {'first_name': 'X'}).status_code, 403)
        self.assertEqual(self.client.get(f'/api/users/students/{self.student.pk}/allergies/').status_code, 403)
        parental = self.client.get(f'/api/users/students/{self.student.pk}/parental-control/')
        self.assertIn(parental.status_code, (403, 404))
        menu = self.client.get(f'/api/catalog/students/{self.student.pk}/monthly_menu/')
        self.assertIn(menu.status_code, (403, 404))

    def test_the_history_keeps_the_removed_child(self):
        past = make_pre_order(self.student, local_today() - timedelta(days=3), PreOrder.Status.DELIVERED)
        self.remove()
        self.assertTrue(PreOrder.objects.filter(pk=past.pk, student=self.student).exists())

    def test_a_removed_child_wallet_cannot_be_recharged(self):
        self.remove()
        wallet = Wallet.objects.select_related('student__user').get(student=self.student)

        class FakeRequest:
            user = self.parent_user

        self.assertFalse(IsWalletOwnerParent().has_object_permission(FakeRequest, None, wallet))

    def test_the_parent_can_register_in_another_sede_once_no_active_child_is_left(self):
        other_sede = Institution.objects.create(name='Sede B')

        def errors():
            serializer = StudentRegistrationSerializer(
                data={
                    'first_name': 'Pedro', 'first_last_name': 'Lopez', 'institution_id': other_sede.pk,
                    'username': 'pedrito', 'password': 'Child-Passw0rd!', 'profile_picture': png_upload(),
                    'accept_terms': True,
                },
                context={'parent_profile': self.parent},
            )
            serializer.is_valid()
            return serializer.errors

        self.assertIn('institution_id', errors())
        self.remove()
        self.assertNotIn('institution_id', errors())

    # --- who can do it --------------------------------------------------------

    def test_a_wrong_password_is_a_400_and_changes_nothing(self):
        response = self.remove(password='mala-clave')
        self.assertEqual(response.status_code, 400)
        self.assertIn('parent_password', response.json())
        self.child.refresh_from_db()
        self.assertTrue(self.child.is_active)

    def test_the_password_is_required(self):
        self.assertEqual(self.client.post(self.url('remove'), {}, format='json').status_code, 400)

    def test_another_parent_cannot_remove_the_child(self):
        other_user, _other = make_family(self.institution, 'z')
        self.authenticate(RefreshToken.for_user(other_user))
        self.assertEqual(self.remove().status_code, 403)
        self.assertEqual(self.check().status_code, 403)
        self.child.refresh_from_db()
        self.assertTrue(self.child.is_active)

    def test_the_child_cannot_remove_themselves(self):
        self.authenticate(self.child_session)
        self.assertEqual(self.remove().status_code, 403)

    def test_attempts_are_rate_limited(self):
        with mock.patch.object(ScopedRateThrottle, 'THROTTLE_RATES', {'child_account': '2/hour'}):
            self.assertEqual(self.remove(password='mala-clave').status_code, 400)
            self.assertEqual(self.remove(password='mala-clave').status_code, 400)
            self.assertEqual(self.remove().status_code, 429)

    # --- conditions -----------------------------------------------------------

    def test_a_balance_blocks_the_removal(self):
        Wallet.objects.filter(student=self.student).update(balance=Decimal('1.50'))
        response = self.remove()
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body['code'], 'child_removal_blocked')
        self.assertEqual(body['blockers'], {'balance': '1.50'})
        self.assertIn('saldo', body['detail'])
        self.child.refresh_from_db()
        self.assertTrue(self.child.is_active)

    def test_a_pending_pre_order_of_today_or_later_blocks_the_removal(self):
        for offset in (0, 4):
            order = make_pre_order(self.student, local_today() + timedelta(days=offset))
            response = self.remove()
            self.assertEqual(response.status_code, 409, offset)
            self.assertEqual(response.json()['blockers'], {'pending_pre_orders': 1})
            order.delete()

    def test_past_or_finished_pre_orders_do_not_block(self):
        make_pre_order(self.student, local_today() - timedelta(days=2))
        make_pre_order(self.student, local_today() + timedelta(days=1), PreOrder.Status.DELIVERED)
        make_pre_order(self.student, local_today() + timedelta(days=2), PreOrder.Status.CANCELED)
        self.assertEqual(self.remove().status_code, 200)

    def test_a_recent_pending_recharge_blocks_the_removal(self):
        make_recharge(self.student.wallet, minutes_ago=10)
        response = self.remove()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['blockers'], {'pending_recharges': 1})

    @override_settings(CHILD_REMOVAL_PENDING_RECHARGE_MINUTES=60)
    def test_an_old_or_finished_recharge_does_not_block(self):
        make_recharge(self.student.wallet, minutes_ago=61)
        make_recharge(self.student.wallet, status=Transaction.Status.SUCCESS)
        make_recharge(self.student.wallet, status=Transaction.Status.FAILED)
        self.assertEqual(self.remove().status_code, 200)

    def test_every_reason_is_reported_together(self):
        Wallet.objects.filter(student=self.student).update(balance=Decimal('3.00'))
        make_pre_order(self.student, local_today())
        make_recharge(self.student.wallet)
        body = self.remove().json()
        self.assertEqual(set(body['blockers']), {'balance', 'pending_pre_orders', 'pending_recharges'})

    def test_the_check_endpoint_tells_whether_the_child_can_be_removed(self):
        self.assertEqual(self.check().json(), {'can_remove': True, 'blockers': {}})

        Wallet.objects.filter(student=self.student).update(balance=Decimal('2.00'))
        body = self.check().json()
        self.assertFalse(body['can_remove'])
        self.assertEqual(body['blockers'], {'balance': '2.00'})
        self.child.refresh_from_db()
        self.assertTrue(self.child.is_active)

    def test_a_child_without_wallet_can_be_removed(self):
        Wallet.objects.filter(student=self.student).delete()
        self.assertEqual(self.remove().status_code, 200)

    def test_removing_one_child_does_not_touch_the_siblings(self):
        sibling = add_child(self.parent, self.institution, 'b', balance='4.00')
        self.remove()
        sibling.user.refresh_from_db()
        self.assertTrue(sibling.user.is_active)
        self.assertEqual(sibling.wallet.balance, Decimal('4.00'))

    def test_a_removed_username_can_be_registered_again(self):
        self.remove()
        self.assertFalse(CustomUser.objects.filter(username='hijoa').exists())
        CustomUser.objects.create_user(username='hijoa', password=CHILD_PASSWORD, role=Role.STUDENT)


class ActiveStudentsQuerySetTests(APITestCase):
    def test_active_leaves_out_removed_students(self):
        institution = Institution.objects.create(name='Sede A')
        _user, parent = make_family(institution, 'q')
        kept = add_child(parent, institution, 'q1')
        gone = add_child(parent, institution, 'q2')
        CustomUser.objects.filter(pk=gone.user_id).update(is_active=False, deleted_at=timezone.now())

        self.assertEqual(list(parent.children.active()), [kept])
        self.assertEqual(parent.children.count(), 2)
