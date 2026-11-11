from datetime import timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status
from rest_framework.test import APITestCase

from catalog.models import Allergen, DailyMenu, MenuDish, MonthlyMenu
from core.timezone import local_today
from users.models import CustomUser, Institution, ParentProfile, StudentProfile

URL = '/api/catalog/students/me/monthly_menu/'


def _dummy_image(name='dish.jpg'):
    return SimpleUploadedFile(
        name=name,
        content=b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b',
        content_type='image/gif',
    )


class OwnStudentMonthlyMenuTests(APITestCase):
    """GET /api/catalog/students/me/monthly_menu/ is read only and student only."""

    def setUp(self):
        self.institution = Institution.objects.create(name='Colegio Propio')
        self.other_institution = Institution.objects.create(name='Colegio Ajeno')

        self.parent_user = CustomUser.objects.create_user(
            username='own_parent', email='own_parent@test.com',
            password='testpassword123', role=CustomUser.Role.PARENT,
        )
        self.parent_profile = ParentProfile.objects.create(user=self.parent_user)
        self.student_user = CustomUser.objects.create_user(
            username='own_student', email='own_student@test.com',
            password='testpassword123', role=CustomUser.Role.STUDENT,
        )
        self.student_profile = StudentProfile.objects.create(
            user=self.student_user, parent=self.parent_profile,
            institution=self.institution, profile_picture=_dummy_image('own.jpg'),
        )
        self.teacher_user = CustomUser.objects.create_user(
            username='own_teacher', email='own_teacher@test.com',
            password='testpassword123', role=CustomUser.Role.TEACHER,
            institution=self.institution,
        )

        self.allergen, _unused = Allergen.objects.get_or_create(name='Maní')
        self.student_profile.allergies.add(self.allergen)

        self.future_date = local_today() + timedelta(days=2)
        self.own_dish = self._publish_dish(self.institution, 'Seco de Pollo', with_allergen=True)
        self.other_dish = self._publish_dish(self.other_institution, 'Plato Ajeno')

    def _publish_dish(self, institution, name, with_allergen=False):
        dish = MenuDish.objects.create(
            institution=institution, name=name, price=Decimal('3.50'),
            image=_dummy_image(f'{name}.jpg'), is_active=True,
        )
        if with_allergen:
            dish.allergens.add(self.allergen)
        monthly = MonthlyMenu.objects.create(
            institution=institution, year=self.future_date.year,
            month=self.future_date.month, status=MonthlyMenu.Status.PUBLISHED,
        )
        day = DailyMenu.objects.create(monthly_menu=monthly, date=self.future_date)
        day.dishes.add(dish)
        return dish

    def test_student_gets_the_menu_of_own_institution(self):
        self.client.force_authenticate(user=self.student_user)
        response = self.client.get(URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        day = next(d for d in response.data if d['date'] == str(self.future_date))
        self.assertFalse(day['is_blocked_by_parental_control'])
        dish_ids = [dish['id'] for dish in day['dishes']]
        self.assertIn(self.own_dish.id, dish_ids)
        self.assertNotIn(self.other_dish.id, dish_ids)

    def test_response_flags_the_students_allergens(self):
        self.client.force_authenticate(user=self.student_user)
        response = self.client.get(URL)
        day = next(d for d in response.data if d['date'] == str(self.future_date))
        dish = next(d for d in day['dishes'] if d['id'] == self.own_dish.id)
        self.assertTrue(dish['has_student_allergens'])

    def test_menu_starts_after_today_like_the_parent_view(self):
        today_monthly = MonthlyMenu.objects.filter(institution=self.institution).first()
        DailyMenu.objects.get_or_create(monthly_menu=today_monthly, date=local_today())
        self.client.force_authenticate(user=self.student_user)
        response = self.client.get(URL)
        self.assertNotIn(str(local_today()), [d['date'] for d in response.data])

    def test_parent_cannot_use_the_student_endpoint(self):
        self.client.force_authenticate(user=self.parent_user)
        self.assertEqual(self.client.get(URL).status_code, status.HTTP_403_FORBIDDEN)

    def test_teacher_cannot_use_the_student_endpoint(self):
        self.client.force_authenticate(user=self.teacher_user)
        self.assertEqual(self.client.get(URL).status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_is_rejected(self):
        self.assertEqual(self.client.get(URL).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_student_without_profile_gets_a_clear_404(self):
        orphan = CustomUser.objects.create_user(
            username='orphan_student', email='orphan@test.com',
            password='testpassword123', role=CustomUser.Role.STUDENT,
        )
        self.client.force_authenticate(user=orphan)
        response = self.client.get(URL)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn('perfil de estudiante', str(response.data['detail']))

    def test_parent_route_is_not_shadowed_by_the_own_route(self):
        self.client.force_authenticate(user=self.parent_user)
        response = self.client.get(f'/api/catalog/students/{self.student_profile.id}/monthly_menu/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
