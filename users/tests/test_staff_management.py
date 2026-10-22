from decimal import Decimal

from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from pos.models import Sale
from users.models import CustomUser, Institution, UserBiometric

PASSWORD = 'test1234'


class StaffManagementTests(APITestCase):
    """Admin rules over staff accounts: operatives are fully managed, teachers
    only (de)activated unless they are still pending activation."""

    def setUp(self):
        self.institution = Institution.objects.create(name='Martim Cereré')
        self.admin = CustomUser.objects.create_user(
            username='admin', email='admin@delycattessen.com', password=PASSWORD,
            role=CustomUser.Role.ADMIN, institution=self.institution,
        )
        self.operative = self._make_user('cajero@delycattessen.com', CustomUser.Role.OPERATIONS_STAFF)
        self.active_teacher = self._make_user('docente@delycattessen.com', CustomUser.Role.TEACHER)
        self.pending_teacher = self._make_user(
            'nuevo@delycattessen.com', CustomUser.Role.TEACHER, pending=True,
        )
        self._auth(self.admin)

    def _make_user(self, email, role, pending=False):
        return CustomUser.objects.create_user(
            username=email, email=email, password=PASSWORD,
            first_name='Ana', last_name='Perez', role=role,
            institution=self.institution, must_change_password=pending,
        )

    def _auth(self, user):
        token = RefreshToken.for_user(user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.access_token}')

    @staticmethod
    def _url(user):
        return f'/api/users/staff/{user.id}/'

    def _login(self, user):
        self.client.credentials()
        return self.client.post(
            '/api/users/login/',
            {'username': user.email, 'email': user.email, 'password': PASSWORD},
            format='json',
        )

    # ---------------------------------------------------------------- operative
    def test_operative_profile_can_be_edited(self):
        response = self.client.patch(self._url(self.operative), {'first_name': 'luis'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.operative.refresh_from_db()
        self.assertEqual(self.operative.first_name, 'LUIS')

    def test_operative_role_cannot_change(self):
        response = self.client.patch(
            self._url(self.operative), {'role': CustomUser.Role.TEACHER}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.operative.refresh_from_db()
        self.assertEqual(self.operative.role, CustomUser.Role.OPERATIONS_STAFF)

    def test_active_operative_email_cannot_change(self):
        response = self.client.patch(
            self._url(self.operative), {'email': 'otro@delycattessen.com'}, format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.operative.refresh_from_db()
        self.assertEqual(self.operative.email, 'cajero@delycattessen.com')

    def test_deactivated_operative_cannot_log_in(self):
        self.assertEqual(self._login(self.operative).status_code, 200)
        self._auth(self.admin)
        response = self.client.patch(self._url(self.operative), {'is_active': False}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self._login(self.operative).status_code, 401)

    def test_deactivated_user_loses_access_with_an_existing_token(self):
        access = RefreshToken.for_user(self.operative).access_token
        self.operative.is_active = False
        self.operative.save(update_fields=['is_active'])
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
        self.assertEqual(self.client.get('/api/users/me/').status_code, 401)

    def test_operative_delete_is_soft_and_keeps_history(self):
        Sale.objects.create(
            institution=self.institution, cashier=self.operative,
            payment_method=Sale.PaymentMethod.CASH, total_amount=Decimal('1.00'),
        )
        UserBiometric.objects.create(
            user=self.operative, encrypted_embedding=b'x', nonce=b'0' * 12, tag=b'0' * 16,
        )
        response = self.client.delete(self._url(self.operative))
        self.assertEqual(response.status_code, 204)

        self.operative.refresh_from_db()
        self.assertIsNotNone(self.operative.deleted_at)
        self.assertFalse(self.operative.is_active)
        self.assertIsNone(self.operative.email)
        self.assertTrue(self.operative.username.startswith('deleted-'))
        self.assertEqual(self.operative.first_name, 'Ana')  # names stay for reports
        self.assertTrue(Sale.objects.filter(cashier=self.operative).exists())
        self.assertFalse(UserBiometric.objects.filter(user=self.operative).exists())

    def test_deleted_account_disappears_from_list_and_detail(self):
        self.client.delete(self._url(self.operative))
        emails = [item['email'] for item in self.client.get('/api/users/staff/').data]
        self.assertNotIn('cajero@delycattessen.com', emails)
        self.assertEqual(self.client.get(self._url(self.operative)).status_code, 404)

    def test_deleted_email_can_be_registered_again(self):
        self.client.delete(self._url(self.operative))
        response = self.client.post('/api/users/staff/', {
            'email': 'cajero@delycattessen.com', 'first_name': 'Luis', 'last_name': 'Mora',
            'role': CustomUser.Role.OPERATIONS_STAFF,
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)

    # ----------------------------------------------------------- active teacher
    def test_active_teacher_profile_cannot_be_edited(self):
        response = self.client.patch(self._url(self.active_teacher), {'first_name': 'Luis'}, format='json')
        self.assertEqual(response.status_code, 403)
        self.active_teacher.refresh_from_db()
        self.assertEqual(self.active_teacher.first_name, 'Ana')

    def test_active_teacher_email_cannot_be_edited(self):
        response = self.client.patch(
            self._url(self.active_teacher), {'email': 'otro@delycattessen.com'}, format='json',
        )
        self.assertEqual(response.status_code, 403)

    def test_active_teacher_cannot_be_deleted(self):
        response = self.client.delete(self._url(self.active_teacher))
        self.assertEqual(response.status_code, 403)
        self.active_teacher.refresh_from_db()
        self.assertIsNone(self.active_teacher.deleted_at)

    def test_active_teacher_can_be_deactivated_and_reactivated(self):
        url = self._url(self.active_teacher)
        self.assertEqual(self.client.patch(url, {'is_active': False}, format='json').status_code, 200)
        self.active_teacher.refresh_from_db()
        self.assertFalse(self.active_teacher.is_active)
        self.assertEqual(self.client.patch(url, {'is_active': True}, format='json').status_code, 200)
        self.active_teacher.refresh_from_db()
        self.assertTrue(self.active_teacher.is_active)

    # ---------------------------------------------------------- pending teacher
    def test_pending_teacher_data_can_be_corrected(self):
        response = self.client.patch(
            self._url(self.pending_teacher),
            {'first_name': 'maria', 'email': 'Corregido@Delycattessen.com'},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.pending_teacher.refresh_from_db()
        self.assertEqual(self.pending_teacher.first_name, 'MARIA')
        self.assertEqual(self.pending_teacher.email, 'corregido@delycattessen.com')
        self.assertEqual(self.pending_teacher.username, 'corregido@delycattessen.com')

    def test_pending_teacher_email_must_be_unique(self):
        response = self.client.patch(
            self._url(self.pending_teacher), {'email': self.operative.email}, format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_pending_teacher_can_be_deleted(self):
        response = self.client.delete(self._url(self.pending_teacher))
        self.assertEqual(response.status_code, 204)
        self.pending_teacher.refresh_from_db()
        self.assertIsNotNone(self.pending_teacher.deleted_at)

    # ------------------------------------------------------------------- list
    def test_list_exposes_pending_activation_flag(self):
        data = {item['email']: item for item in self.client.get('/api/users/staff/').data}
        self.assertTrue(data['nuevo@delycattessen.com']['must_change_password'])
        self.assertFalse(data['docente@delycattessen.com']['must_change_password'])