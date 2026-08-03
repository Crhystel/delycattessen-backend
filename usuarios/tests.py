from rest_framework.test import APITestCase
from .models import CustomUser, Location


class LocationIsolationTests(APITestCase):
    def setUp(self):
        self.location_a = Location.objects.create(name="Martim Cereré")
        self.location_b = Location.objects.create(name="El Sauce")
        self.admin_a = CustomUser.objects.create_user(
            username="admin_a", email="admin_a@delycattessen.com",
            password="test1234", role=CustomUser.Role.ADMIN, location=self.location_a
        )
        self.staff_b = CustomUser.objects.create_user(
            username="staff_b", email="staff_b@delycattessen.com",
            password="test1234", role=CustomUser.Role.OPERATIONS_STAFF, location=self.location_b
        )

    def test_users_belong_to_their_corresponding_location(self):
        self.assertEqual(self.admin_a.location, self.location_a)
        self.assertNotEqual(self.admin_a.location, self.staff_b.location)

    def test_administrator_does_not_see_staff_from_another_location(self):
        from .mixins import LocationIsolationMixin

        class FakeView:
            def __init__(self, request):
                self.request = request
            def get_queryset(self):
                return CustomUser.objects.filter(role=CustomUser.Role.OPERATIONS_STAFF)

        class ViewWithMixin(LocationIsolationMixin, FakeView):
            pass

        class FakeRequest:
            user = self.admin_a

        view = ViewWithMixin(FakeRequest())
        result = view.get_queryset()
        self.assertNotIn(self.staff_b, result)