from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from catalog.models import Allergen
from users.models import CustomUser, Institution, ParentProfile, StudentProfile

GIF = (
    b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04'
    b'\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
)


class AllergyViewsTests(APITestCase):
    def setUp(self):
        make = CustomUser.objects.create_user
        institution = Institution.objects.create(name='Sede A')
        self.teacher = make(username='teacher', email='t@x.com', password='x',
                            role=CustomUser.Role.TEACHER, institution=institution)
        self.parent_user = make(username='parent', email='p@x.com', password='x', role=CustomUser.Role.PARENT)
        self.parent = ParentProfile.objects.create(user=self.parent_user)
        student_user = make(username='student', email='s@x.com', password='x', role=CustomUser.Role.STUDENT)
        self.student = StudentProfile.objects.create(
            user=student_user, parent=self.parent, institution=institution,
            profile_picture=SimpleUploadedFile('s.gif', GIF, content_type='image/gif'))
        self.gluten = Allergen.objects.create(name='Gluten test')
        self.soy = Allergen.objects.create(name='Soya test')

    def _auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def test_teacher_manages_own_allergies(self):
        self._auth(self.teacher)
        response = self.client.put('/api/users/allergies/', {'allergen_ids': [self.gluten.id]}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['unprotected_allergens'], [self.gluten.name])
        self.assertEqual(list(self.teacher.allergies.all()), [self.gluten])
        listed = self.client.get('/api/users/allergies/')
        self.assertEqual([row['name'] for row in listed.data], [self.gluten.name])

    def test_teacher_allergies_replace_previous_ones(self):
        self.teacher.allergies.add(self.gluten)
        self._auth(self.teacher)
        self.client.put('/api/users/allergies/', {'allergen_ids': [self.soy.id]}, format='json')
        self.assertEqual(list(self.teacher.allergies.all()), [self.soy])

    def test_invalid_allergen_ids_are_rejected(self):
        self._auth(self.teacher)
        for ids in ([9999], [self.gluten.id, 9999], 'abc'):
            response = self.client.put('/api/users/allergies/', {'allergen_ids': ids}, format='json')
            self.assertEqual(response.status_code, 400, ids)

    def test_only_teachers_use_the_own_allergies_endpoint(self):
        for user in (self.parent_user, self.student.user):
            self._auth(user)
            self.assertEqual(self.client.get('/api/users/allergies/').status_code, 403)

    def test_parent_still_manages_child_allergies(self):
        self._auth(self.parent_user)
        url = f'/api/users/students/{self.student.id}/allergies/'
        response = self.client.put(url, {'allergen_ids': [self.gluten.id]}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(self.student.allergies.all()), [self.gluten])
        self.assertEqual(len(self.client.get(url).data), 1)
        self.assertEqual(self.client.put(url, {'allergen_ids': [9999]}, format='json').status_code, 400)
