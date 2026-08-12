#Pruebas de integración del alcance por institución en el endpoint de Staff.
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken
from usuarios.models import CustomUser, Institution


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
        """Caso: admin sin parámetro ve solo su institución por defecto."""
        self._auth(self.admin_a)
        response = self.client.get('/api/usuarios/staff/')
        emails = [item['email'] for item in response.data]
        self.assertIn(self.staff_a.email, emails)
        self.assertNotIn(self.staff_b.email, emails)

    def test_admin_can_view_another_institution_explicitly(self):
        """Caso: admin solicita explícitamente otra institución."""
        self._auth(self.admin_a)
        response = self.client.get(f'/api/usuarios/staff/?institution={self.institution_b.id}')
        emails = [item['email'] for item in response.data]
        self.assertIn(self.staff_b.email, emails)
        self.assertNotIn(self.staff_a.email, emails)

    def test_admin_can_view_consolidated_across_institutions(self):
        """Caso: admin solicita vista consolidada de todas las instituciones."""
        self._auth(self.admin_a)
        response = self.client.get('/api/usuarios/staff/?institution=all')
        emails = [item['email'] for item in response.data]
        self.assertIn(self.staff_a.email, emails)
        self.assertIn(self.staff_b.email, emails)

    def test_unauthenticated_request_is_rejected(self):
        """Caso negativo: sin token, se rechaza con 401."""
        response = self.client.get('/api/usuarios/staff/')
        self.assertEqual(response.status_code, 401)
        
    def test_admin_can_list_institutions(self):
        """Caso: admin obtiene la lista de instituciones para el selector."""
        self._auth(self.admin_a)
        response = self.client.get('/api/usuarios/institutions/')
        self.assertEqual(response.status_code, 200)
        names = [item['name'] for item in response.data]
        self.assertIn(self.institution_a.name, names)
        self.assertIn(self.institution_b.name, names)