from unittest import mock

import numpy as np
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from users.models import CustomUser, Institution, ParentProfile, StudentProfile, TermsAcceptance

GIF = (
    b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04'
    b'\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
)
EXTRACT = 'core.biometrics.BiometricValidationMixin.extract_face_embedding'
Document = TermsAcceptance.Document


def _photo(name='f.gif'):
    return SimpleUploadedFile(name, GIF, content_type='image/gif')


def _face():
    vector = np.zeros(128, dtype=np.float32)
    vector[0] = 1.0
    return vector


@override_settings(TERMS_VERSION='2026-11')
class TermsAcceptanceTests(APITestCase):
    """Registering a parent or a child, or changing a child's photo, requires the
    terms checkbox and stores who accepted which version and when."""

    def setUp(self):
        self.institution = Institution.objects.create(name='Sede A')
        self.parent_user = CustomUser.objects.create_user(
            username='parent', email='p@x.com', password='x', role=CustomUser.Role.PARENT)
        self.parent = ParentProfile.objects.create(user=self.parent_user)

    def _auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    # ---- parent registration -----------------------------------------------
    def _register_parent(self, **extra):
        payload = {'email': 'nuevo@x.com', 'password': 'Str0ngPass!234', 'password_confirm': 'Str0ngPass!234'}
        return self.client.post('/api/users/parent-registration/', {**payload, **extra}, format='json')

    def test_parent_registration_requires_accepting_the_terms(self):
        for extra in ({}, {'accept_terms': False}):
            with self.subTest(extra=extra):
                response = self._register_parent(**extra)
                self.assertEqual(response.status_code, 400)
                self.assertIn('accept_terms', response.data)
        self.assertFalse(CustomUser.objects.filter(email='nuevo@x.com').exists())

    def test_parent_registration_stores_the_accepted_version(self):
        response = self._register_parent(accept_terms=True)

        self.assertEqual(response.status_code, 201, response.data)
        acceptance = TermsAcceptance.objects.get()
        self.assertEqual(
            (acceptance.user.email, acceptance.document, acceptance.version, acceptance.ip),
            ('nuevo@x.com', Document.PARENT_REGISTRATION, '2026-11', '127.0.0.1'),
        )

    # ---- child registration --------------------------------------------------
    def _register_child(self, **extra):
        self._auth(self.parent_user)
        payload = {
            'first_name': 'Luis', 'first_last_name': 'Gomez', 'institution_id': self.institution.id,
            'username': 'luis', 'password': 'Str0ngPass!234', 'profile_picture': _photo('luis.gif'), **extra,
        }
        with mock.patch(EXTRACT, return_value=_face()):
            return self.client.post('/api/users/student-registration/', payload, format='multipart')

    def test_child_registration_requires_and_stores_the_acceptance(self):
        self.assertEqual(self._register_child().status_code, 400)
        self.assertFalse(StudentProfile.objects.exists())

        response = self._register_child(accept_terms='true')

        self.assertEqual(response.status_code, 201, response.data)
        acceptance = TermsAcceptance.objects.get()
        self.assertEqual(
            (acceptance.user, acceptance.document, acceptance.student.user.username, acceptance.version),
            (self.parent_user, Document.STUDENT_REGISTRATION, 'luis', '2026-11'),
        )

    # ---- child photo -------------------------------------------------------------
    def test_photo_change_requires_and_stores_the_acceptance(self):
        user = CustomUser.objects.create_user(
            username='ana', email='ana@x.com', password='x', role=CustomUser.Role.STUDENT)
        child = StudentProfile.objects.create(
            user=user, parent=self.parent, institution=self.institution, profile_picture=_photo('ana.gif'))
        self._auth(self.parent_user)
        url = f'/api/users/children/{child.id}/photo/'

        with mock.patch(EXTRACT, return_value=_face()):
            rejected = self.client.patch(url, {'profile_picture': _photo('new.gif')}, format='multipart')
            accepted = self.client.patch(
                url, {'profile_picture': _photo('new.gif'), 'accept_terms': 'true'}, format='multipart')

        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(accepted.status_code, 200, accepted.data)
        acceptance = TermsAcceptance.objects.get()
        self.assertEqual((acceptance.document, acceptance.student), (Document.CHILD_PHOTO, child))
