from rest_framework.test import APITestCase
from .models import CustomUser, Institution


class InstitutionIsolationTests(APITestCase):
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

    def test_administrator_does_not_see_staff_from_another_institution(self):
        from .mixins import InstitutionIsolationMixin

        class FakeView:
            def __init__(self, request):
                self.request = request
            def get_queryset(self):
                return CustomUser.objects.filter(role=CustomUser.Role.OPERATIONS_STAFF)

        class ViewWithMixin(InstitutionIsolationMixin, FakeView):
            pass

        class FakeRequest:
            user = self.admin_a

        view = ViewWithMixin(FakeRequest())
        result = view.get_queryset()
        self.assertNotIn(self.staff_b, result)