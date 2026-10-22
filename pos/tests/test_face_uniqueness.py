from unittest import mock

import numpy as np
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from core.biometrics import BiometricValidationMixin, FaceAlreadyRegistered
from users.models import CustomUser, Institution, ParentProfile, StudentProfile, UserBiometric

GIF = (
    b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04'
    b'\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
)
EXTRACT = 'core.biometrics.BiometricValidationMixin.extract_face_embedding'


def _photo(name='f.gif'):
    return SimpleUploadedFile(name, GIF, content_type='image/gif')


def _unit(index, noise=0.0):
    """Unit vector along one axis (plus optional small noise): axes are orthogonal faces."""
    vector = np.zeros(128, dtype=np.float32)
    vector[index] = 1.0
    if noise:
        vector[(index + 1) % 128] = noise
    return vector / np.linalg.norm(vector)


class FaceUniquenessTests(APITestCase):
    def setUp(self):
        self.mixin = BiometricValidationMixin()
        self.inst_a = Institution.objects.create(name='Sede A')
        self.inst_b = Institution.objects.create(name='Sede B')
        self.parent_user = CustomUser.objects.create_user(
            username='parent', email='p@x.com', password='x', role=CustomUser.Role.PARENT)
        self.parent = ParentProfile.objects.create(user=self.parent_user)
        self.child1 = self._child('child1', self.inst_a)
        self.child2 = self._child('child2', self.inst_a)

    def _child(self, username, institution):
        user = CustomUser.objects.create_user(
            username=username, email=f'{username}@x.com', password='x', role=CustomUser.Role.STUDENT)
        return StudentProfile.objects.create(
            user=user, parent=self.parent, institution=institution, profile_picture=_photo(f'{username}.gif'))

    def _store(self, user, vector):
        ciphertext, nonce, tag = self.mixin.encrypt_embedding(vector)
        return UserBiometric.objects.update_or_create(
            user=user,
            defaults={'encrypted_embedding': ciphertext, 'nonce': nonce, 'tag': tag, 'is_active': True},
        )[0]

    def _auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    # ---- POS matching -------------------------------------------------
    def test_identical_vectors_are_ambiguous_and_identify_nobody(self):
        """Two kids with the same face vector: the POS must not guess the first one."""
        self._store(self.child1.user, _unit(0))
        self._store(self.child2.user, _unit(0))
        user, score = self.mixin.match_face(_unit(0), institution=self.inst_a)
        self.assertIsNone(user)
        self.assertGreater(score, 0.99)

    def test_clearly_best_match_wins_regardless_of_order(self):
        self._store(self.child1.user, _unit(0))
        self._store(self.child2.user, _unit(1))
        self.assertEqual(self.mixin.match_face(_unit(1), institution=self.inst_a)[0], self.child2.user)
        self.assertEqual(self.mixin.match_face(_unit(0), institution=self.inst_a)[0], self.child1.user)

    def test_unknown_face_is_not_identified(self):
        self._store(self.child1.user, _unit(0))
        self.assertIsNone(self.mixin.match_face(_unit(5), institution=self.inst_a)[0])

    # ---- duplicate guard -------------------------------------------------
    def test_ensure_face_not_registered(self):
        self._store(self.child1.user, _unit(0))
        with self.assertRaises(FaceAlreadyRegistered):
            self.mixin.ensure_face_not_registered(_unit(0, noise=0.05), self.inst_a)
        # owner changing their own photo is fine
        self.mixin.ensure_face_not_registered(_unit(0), self.inst_a, exclude_user=self.child1.user)
        # a different face is fine
        self.mixin.ensure_face_not_registered(_unit(7), self.inst_a)
        # the same face in another sede is fine (data is separated per sede)
        self.mixin.ensure_face_not_registered(_unit(0), self.inst_b)

    # ---- photo update -------------------------------------------------
    def _patch_photo(self, child, **extract):
        self._auth(self.parent_user)
        with mock.patch(EXTRACT, **extract):
            return self.client.patch(
                f'/api/users/children/{child.id}/photo/', {'profile_picture': _photo('new.gif')}, format='multipart')

    def test_photo_update_replaces_embedding_and_photo(self):
        self._store(self.child1.user, _unit(0))
        self._store(self.child2.user, _unit(1))
        response = self._patch_photo(self.child1, return_value=_unit(2))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data['biometric_registered'])
        self.assertEqual(self.mixin.match_face(_unit(2), institution=self.inst_a)[0], self.child1.user)
        self.assertEqual(self.mixin.match_face(_unit(1), institution=self.inst_a)[0], self.child2.user)

    def test_photo_update_without_face_fails_and_keeps_old_data(self):
        stored = self._store(self.child1.user, _unit(0))
        old_embedding = bytes(stored.encrypted_embedding)
        old_picture = self.child1.profile_picture.name
        response = self._patch_photo(self.child1, side_effect=ValidationError('No se detectó un rostro claro'))
        self.assertEqual(response.status_code, 400)
        self.child1.refresh_from_db()
        stored.refresh_from_db()
        self.assertEqual(self.child1.profile_picture.name, old_picture)
        self.assertEqual(bytes(stored.encrypted_embedding), old_embedding)
        self.assertEqual(self.mixin.match_face(_unit(0), institution=self.inst_a)[0], self.child1.user)

    def test_photo_update_with_face_of_sibling_is_rejected(self):
        self._store(self.child1.user, _unit(0))
        self._store(self.child2.user, _unit(1))
        response = self._patch_photo(self.child1, return_value=_unit(1))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.mixin.match_face(_unit(0), institution=self.inst_a)[0], self.child1.user)

    def test_photo_update_with_own_face_is_allowed(self):
        self._store(self.child1.user, _unit(0))
        self.assertEqual(self._patch_photo(self.child1, return_value=_unit(0, noise=0.05)).status_code, 200)

    # ---- registration ---------------------------------------------------
    def test_registration_with_registered_face_is_rejected_before_creating_anything(self):
        self._store(self.child1.user, _unit(0))
        self._auth(self.parent_user)
        payload = {
            'first_name': 'Luis', 'first_last_name': 'Gomez', 'institution_id': self.inst_a.id,
            'username': 'luis_new', 'password': 'Str0ngPass!234', 'profile_picture': _photo('luis.gif'),
        }
        with mock.patch(EXTRACT, return_value=_unit(0)):
            response = self.client.post('/api/users/student-registration/', payload, format='multipart')
        self.assertEqual(response.status_code, 409, getattr(response, 'data', None))
        self.assertFalse(CustomUser.objects.filter(username='luis_new').exists())
