import numpy as np
from django.test import TestCase

from core.biometrics import BiometricValidationMixin, FaceAlreadyRegistered
from users.models import CustomUser, Institution, UserBiometric


class InactiveUserFaceMatchTests(TestCase):
    """A deactivated account must not be recognized at the POS, but its face
    keeps blocking duplicate registrations (the deactivation is reversible)."""

    def setUp(self):
        self.mixin = BiometricValidationMixin()
        self.institution = Institution.objects.create(name='Martim Cereré')
        self.teacher = CustomUser.objects.create_user(
            username='docente@delycattessen.com', email='docente@delycattessen.com',
            password='test1234', role=CustomUser.Role.TEACHER, institution=self.institution,
        )
        vector = np.random.default_rng(1).normal(size=128).astype(np.float32)
        self.vector = vector / np.linalg.norm(vector)
        ciphertext, nonce, tag = self.mixin.encrypt_embedding(self.vector)
        UserBiometric.objects.create(
            user=self.teacher, encrypted_embedding=ciphertext, nonce=nonce, tag=tag, is_active=True,
        )

    def test_active_teacher_is_recognized(self):
        matched, _score = self.mixin.match_face(self.vector, institution=self.institution)
        self.assertEqual(matched, self.teacher)

    def test_deactivated_teacher_is_not_recognized(self):
        self.teacher.is_active = False
        self.teacher.save(update_fields=['is_active'])
        matched, _score = self.mixin.match_face(self.vector, institution=self.institution)
        self.assertIsNone(matched)

    def test_deactivated_teacher_face_still_blocks_duplicates(self):
        self.teacher.is_active = False
        self.teacher.save(update_fields=['is_active'])
        with self.assertRaises(FaceAlreadyRegistered):
            self.mixin.ensure_face_not_registered(self.vector, self.institution)