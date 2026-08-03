from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken
from .models import CustomUser, Institution


class InstitutionIsolationTests(APITestCase):
    def setUp(self):
        self.institution_a = Institution.objects.create(name="Martim Cereré")
        self.institution_b = Institution.objects.create(name="El Sauce")
        self.admin_a = CustomUser.objects.create_user(
            username="admin_a", email="admin_a@delycattessen.com",
            password="test1234", role=CustomUser.Role.ADMIN, institution=self.institution_a
        )
        self.admin_b = CustomUser.objects.create_user(
            username="admin_b", email="admin_b@delycattessen.com",
            password="test1234", role=CustomUser.Role.ADMIN, institution=self.institution_b
        )
        self.staff_a = CustomUser.objects.create_user(
            username="staff_a", email="staff_a@delycattessen.com",
            password="test1234", role=CustomUser.Role.TEACHER, institution=self.institution_a
        )

    def test_users_belong_to_their_corresponding_institution(self):
        self.assertEqual(self.admin_a.institution, self.institution_a)
        self.assertNotEqual(self.admin_a.institution, self.admin_b.institution)

    def test_administrator_does_not_see_staff_from_another_institution_via_api(self):
        """
        Prueba de integración real: verifica el ViewSet completo a través
        del endpoint HTTP, no solo el Mixin de forma aislada.
        """
        token = RefreshToken.for_user(self.admin_b)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.access_token}')

        response = self.client.get('/api/usuarios/staff/')

        self.assertEqual(response.status_code, 200)
        emails_returned = [item['email'] for item in response.data]
        self.assertNotIn(self.staff_a.email, emails_returned)