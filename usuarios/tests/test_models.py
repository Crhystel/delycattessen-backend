#Pruebas Unitarias
from django.test import TestCase
from usuarios.models import CustomUser, Institution


class InstitutionModelTests(TestCase):
    def test_institution_name_is_unique(self):
        Institution.objects.create(name="Martim Cereré")
        with self.assertRaises(Exception):
            Institution.objects.create(name="Martim Cereré")


class CustomUserModelTests(TestCase):
    def setUp(self):
        self.institution_a = Institution.objects.create(name="Martim Cereré")
        self.institution_b = Institution.objects.create(name="El Sauce")
        self.admin_a = CustomUser.objects.create_user(
            username="admin_a", email="admin_a@delycattessen.com",
            password="test1234", role=CustomUser.Role.ADMIN, institution=self.institution_a
        )
        self.staff_b = CustomUser.objects.create_user(
            username="staff_b", email="staff_b@delycattessen.com",
            password="test1234", role=CustomUser.Role.OPERATIONS_STAFF, institution=self.institution_b
        )

    def test_users_belong_to_their_corresponding_institution(self):
        self.assertEqual(self.admin_a.institution, self.institution_a)
        self.assertNotEqual(self.admin_a.institution, self.staff_b.institution)

    def test_default_role_is_student(self):
        default_user = CustomUser.objects.create_user(
            username="default_user", email="default@delycattessen.com", password="test1234"
        )
        self.assertEqual(default_user.role, CustomUser.Role.STUDENT)