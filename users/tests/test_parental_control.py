from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from users.models import CustomUser, Institution, ParentProfile, ParentalControl, StudentProfile

GIF = (
    b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04'
    b'\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
)
CHILDREN = '/api/users/children/'


class ParentalControlTests(APITestCase):
    """The parent configures spending limits, allowed days and the low balance alert."""

    def setUp(self):
        make = CustomUser.objects.create_user
        institution = Institution.objects.create(name='Sede A')
        self.parent_user = make(username='parent', email='p@x.com', password='x', role=CustomUser.Role.PARENT)
        parent = ParentProfile.objects.create(user=self.parent_user)
        self.other_parent_user = make(username='other', email='o@x.com', password='x', role=CustomUser.Role.PARENT)
        ParentProfile.objects.create(user=self.other_parent_user)
        student_user = make(username='student', email=None, password='x', role=CustomUser.Role.STUDENT,
                            first_name='ANA', last_name='PEREZ')
        self.student = StudentProfile.objects.create(
            user=student_user, parent=parent, institution=institution,
            profile_picture=SimpleUploadedFile('s.gif', GIF, content_type='image/gif'))
        self.url = f'/api/users/students/{self.student.id}/parental-control/'

    def _auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    # ---- low balance alert settings -------------------------------------------
    def test_alert_is_off_with_a_default_amount(self):
        self._auth(self.parent_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data['low_balance_alert_enabled'])
        self.assertEqual(response.data['low_balance_alert_amount'], '2.00')

    def test_parent_turns_the_alert_on_with_an_amount(self):
        self._auth(self.parent_user)
        response = self.client.put(self.url, {
            'daily_limit_enabled': False, 'daily_limit_amount': '0.00',
            'allowed_days_enabled': False, 'allowed_days': [],
            'low_balance_alert_enabled': True, 'low_balance_alert_amount': '3.50',
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        control = ParentalControl.objects.get(student=self.student)
        self.assertTrue(control.low_balance_alert_enabled)
        self.assertEqual(str(control.low_balance_alert_amount), '3.50')

    def test_an_old_client_without_the_new_fields_still_saves(self):
        self._auth(self.parent_user)
        response = self.client.put(self.url, {
            'daily_limit_enabled': True, 'daily_limit_amount': '5.00',
            'allowed_days_enabled': True, 'allowed_days': [0, 1],
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        control = ParentalControl.objects.get(student=self.student)
        self.assertFalse(control.low_balance_alert_enabled)

    def test_negative_alert_amount_is_rejected(self):
        self._auth(self.parent_user)
        response = self.client.patch(self.url, {'low_balance_alert_amount': '-1.00'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('low_balance_alert_amount', response.data)

    def test_another_parent_cannot_read_or_change_it(self):
        self._auth(self.other_parent_user)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(
            self.client.patch(self.url, {'low_balance_alert_enabled': True}, format='json').status_code, 404)

    # ---- children list ----------------------------------------------------------
    def test_children_list_has_no_alert_amount_by_default(self):
        self._auth(self.parent_user)
        response = self.client.get(CHILDREN)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data[0]['low_balance_alert_amount'])

    def test_children_list_shows_the_amount_only_while_the_alert_is_on(self):
        ParentalControl.objects.create(
            student=self.student, low_balance_alert_enabled=True, low_balance_alert_amount='4.00')
        self._auth(self.parent_user)
        self.assertEqual(self.client.get(CHILDREN).data[0]['low_balance_alert_amount'], '4.00')

        ParentalControl.objects.filter(student=self.student).update(low_balance_alert_enabled=False)
        self.assertIsNone(self.client.get(CHILDREN).data[0]['low_balance_alert_amount'])
