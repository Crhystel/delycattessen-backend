#Integration tests for institution scoping on the Staff endpoint.
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken
from users.models import CustomUser, Institution


class InstitutionScopeTests(APITestCase):
    def setUp(self):
        self.institution_a = Institution.objects.create(name="Martim Cereré")
        self.institution_b = Institution.objects.create(name="El Sauce")
        self.admin_a = CustomUser.objects.create_user(
            username="admin_a", email="admin_a@delycattessen.com",
            password="test1234", role=CustomUser.Role.ADMIN, institution=self.institution_a
        )
        self.staff_a = CustomUser.objects.create_user(
            username="staff_a", email="staff_a@delycattessen.com",
            password="test1234", role=CustomUser.Role.TEACHER, institution=self.institution_a
        )
        self.staff_b = CustomUser.objects.create_user(
            username="staff_b", email="staff_b@delycattessen.com",
            password="test1234", role=CustomUser.Role.TEACHER, institution=self.institution_b
        )

    def _auth(self, user):
        token = RefreshToken.for_user(user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.access_token}')

    def test_admin_default_view_shows_own_institution_only(self):
        """Case: admin with no parameter sees only their own institution by default."""
        self._auth(self.admin_a)
        response = self.client.get('/api/users/staff/')
        emails = [item['email'] for item in response.data]
        self.assertIn(self.staff_a.email, emails)
        self.assertNotIn(self.staff_b.email, emails)

    def test_admin_can_view_another_institution_explicitly(self):
        """Case: admin explicitly requests another institution."""
        self._auth(self.admin_a)
        response = self.client.get(f'/api/users/staff/?institution={self.institution_b.id}')
        emails = [item['email'] for item in response.data]
        self.assertIn(self.staff_b.email, emails)
        self.assertNotIn(self.staff_a.email, emails)

    def test_admin_can_view_consolidated_across_institutions(self):
        """Case: admin requests a consolidated view across all institutions."""
        self._auth(self.admin_a)
        response = self.client.get('/api/users/staff/?institution=all')
        emails = [item['email'] for item in response.data]
        self.assertIn(self.staff_a.email, emails)
        self.assertIn(self.staff_b.email, emails)

    def test_unauthenticated_request_is_rejected(self):
        """Negative case: without a token, the request is rejected with 401."""
        response = self.client.get('/api/users/staff/')
        self.assertEqual(response.status_code, 401)

    def _create_teacher(self, email, suffix='', **extra):
        payload = {
            'email': email, 'first_name': 'Ana', 'last_name': 'Perez',
            'role': CustomUser.Role.TEACHER, **extra,
        }
        return self.client.post(f'/api/users/staff/{suffix}', payload, format='json')

    def test_admin_creates_staff_in_targeted_institution(self):
        """Case: ?institution=<id> on create puts the new user in that sede."""
        self._auth(self.admin_a)
        response = self._create_teacher('new_b@delycattessen.com', f'?institution={self.institution_b.id}')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['institution'], self.institution_b.name)
        self.assertEqual(CustomUser.objects.get(email='new_b@delycattessen.com').institution, self.institution_b)

    def test_admin_creates_staff_in_own_institution_without_target_or_with_all(self):
        """Case: no param or 'all' on a write falls back to the admin's own sede."""
        self._auth(self.admin_a)
        for i, suffix in enumerate(('', '?institution=all')):
            email = f'own_{i}@delycattessen.com'
            self.assertEqual(self._create_teacher(email, suffix).status_code, 201)
            self.assertEqual(CustomUser.objects.get(email=email).institution, self.institution_a)

    def test_body_institution_id_wins_over_query_param(self):
        """Case: backward compatibility, institution_id in the body is honored first."""
        self._auth(self.admin_a)
        response = self._create_teacher(
            'body@delycattessen.com', f'?institution={self.institution_a.id}',
            institution_id=self.institution_b.id,
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(CustomUser.objects.get(email='body@delycattessen.com').institution, self.institution_b)

    def test_create_staff_with_invalid_institution_is_400(self):
        self._auth(self.admin_a)
        self.assertEqual(self._create_teacher('bad@delycattessen.com', '?institution=9999').status_code, 400)

    def test_admin_edits_and_deletes_other_sede_staff_only_when_targeting_it(self):
        """Case: PATCH/DELETE on another sede's user needs ?institution=<id>."""
        self._auth(self.admin_a)
        url = f'/api/users/staff/{self.staff_b.id}/'
        for suffix in ('', '?institution=all'):
            self.assertEqual(self.client.patch(url + suffix, {'first_name': 'X'}, format='json').status_code, 404)
            self.assertEqual(self.client.delete(url + suffix).status_code, 404)
        target = f'?institution={self.institution_b.id}'
        self.assertEqual(self.client.patch(url + target, {'first_name': 'Nuevo'}, format='json').status_code, 200)
        self.assertEqual(self.client.delete(url + target).status_code, 204)

    def test_admin_can_list_institutions(self):
        """Case: admin gets the institution list for the selector."""
        self._auth(self.admin_a)
        response = self.client.get('/api/users/institutions/')
        self.assertEqual(response.status_code, 200)
        names = [item['name'] for item in response.data]
        self.assertIn(self.institution_a.name, names)
        self.assertIn(self.institution_b.name, names)