from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from catalog.models import Allergen
from users.models import CustomUser, Institution, ParentProfile, StudentProfile

GIF = (
    b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04'
    b'\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
)
URL = '/api/users/me/allergies/'


def _picture(name):
    return SimpleUploadedFile(name, GIF, content_type='image/gif')


class OwnStudentAllergyTests(APITestCase):
    """GET /api/users/me/allergies/ is read only and student only."""

    def setUp(self):
        make = CustomUser.objects.create_user
        institution = Institution.objects.create(name='Sede A')
        self.parent_user = make(username='parent', email='p@x.com', password='x', role=CustomUser.Role.PARENT)
        parent = ParentProfile.objects.create(user=self.parent_user)
        self.student_user = make(username='student', email='s@x.com', password='x',
                                 role=CustomUser.Role.STUDENT, institution=institution)
        self.student = StudentProfile.objects.create(
            user=self.student_user, parent=parent, institution=institution, profile_picture=_picture('a.gif'))
        sibling_user = make(username='sibling', email='s2@x.com', password='x',
                            role=CustomUser.Role.STUDENT, institution=institution)
        self.sibling = StudentProfile.objects.create(
            user=sibling_user, parent=parent, institution=institution, profile_picture=_picture('b.gif'))
        self.teacher = make(username='teacher', email='t@x.com', password='x',
                            role=CustomUser.Role.TEACHER, institution=institution)
        self.gluten = Allergen.objects.create(name='Gluten propio')
        self.soy = Allergen.objects.create(name='Soya propia')

    def _auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def test_student_reads_only_their_own_allergies(self):
        self.student.allergies.add(self.gluten)
        self.sibling.allergies.add(self.soy)
        self._auth(self.student_user)
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data,
            [{'id': self.gluten.id, 'name': 'Gluten propio'}],
        )

    def test_student_without_allergies_gets_an_empty_list(self):
        self._auth(self.student_user)
        response = self.client.get(URL)
        self.assertEqual((response.status_code, response.data), (200, []))

    def test_student_cannot_write_their_allergies(self):
        self.student.allergies.add(self.gluten)
        self._auth(self.student_user)
        for method in ('put', 'patch', 'post', 'delete'):
            response = getattr(self.client, method)(URL, {'allergen_ids': []}, format='json')
            self.assertEqual(response.status_code, 405, method)
        self.assertEqual(list(self.student.allergies.all()), [self.gluten])

    def test_parent_and_teacher_are_forbidden(self):
        for user in (self.parent_user, self.teacher):
            self._auth(user)
            self.assertEqual(self.client.get(URL).status_code, 403)

    def test_anonymous_is_unauthorized(self):
        self.assertEqual(self.client.get(URL).status_code, 401)

    def test_student_account_without_profile_gets_404(self):
        orphan = CustomUser.objects.create_user(
            username='orphan', email='o@x.com', password='x', role=CustomUser.Role.STUDENT)
        self._auth(orphan)
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(str(response.data['detail']), 'Tu cuenta no tiene un perfil de estudiante.')

    def test_teacher_and_parent_routes_are_unchanged(self):
        self.teacher.allergies.add(self.soy)
        self._auth(self.teacher)
        self.assertEqual([row['name'] for row in self.client.get('/api/users/allergies/').data], ['Soya propia'])
        self._auth(self.parent_user)
        url = f'/api/users/students/{self.student.id}/allergies/'
        self.assertEqual(self.client.put(url, {'allergen_ids': [self.gluten.id]}, format='json').status_code, 200)
