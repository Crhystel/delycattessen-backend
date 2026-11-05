import numpy as np
from django.test import TestCase

from core.biometrics import BiometricValidationMixin, FaceAlreadyRegistered
from users.models import CustomUser, Institution, ParentProfile, StudentProfile, UserBiometric


class InactiveUserFaceMatchTests(TestCase):
    """A deactivated account must not be recognized at the POS, but its face
    keeps blocking duplicate registrations (the deactivation is reversible)."""

    def setUp(self):
        self.mixin = BiometricValidationMixin()
        self.institution = Institution.objects.create(name='Martim Cereré')
        parent_user = CustomUser.objects.create_user(
            username='padre@delycattessen.com', email='padre@delycattessen.com',
            password='test1234', role=CustomUser.Role.PARENT,
        )
        self.student = CustomUser.objects.create_user(
            username='estudiante', password='test1234',
            role=CustomUser.Role.STUDENT, institution=self.institution,
        )
        StudentProfile.objects.create(
            user=self.student, institution=self.institution,
            parent=ParentProfile.objects.create(user=parent_user),
        )
        vector = np.random.default_rng(1).normal(size=128).astype(np.float32)
        self.vector = vector / np.linalg.norm(vector)
        ciphertext, nonce, tag = self.mixin.encrypt_embedding(self.vector)
        UserBiometric.objects.create(
            user=self.student, encrypted_embedding=ciphertext, nonce=nonce, tag=tag, is_active=True,
        )

    def test_active_student_is_recognized(self):
        matched, _score = self.mixin.match_face(self.vector, institution=self.institution)
        self.assertEqual(matched, self.student)

    def test_deactivated_student_is_not_recognized(self):
        self.student.is_active = False
        self.student.save(update_fields=['is_active'])
        matched, _score = self.mixin.match_face(self.vector, institution=self.institution)
        self.assertIsNone(matched)

    def test_deactivated_student_face_still_blocks_duplicates(self):
        self.student.is_active = False
        self.student.save(update_fields=['is_active'])
        with self.assertRaises(FaceAlreadyRegistered):
            self.mixin.ensure_face_not_registered(self.vector, self.institution)

    def test_teacher_faces_are_never_matched(self):
        """Teachers have no biometrics: a stray record is ignored at the POS and
        does not block anyone else's registration."""
        teacher = CustomUser.objects.create_user(
            username='docente@delycattessen.com', email='docente@delycattessen.com',
            password='test1234', role=CustomUser.Role.TEACHER, institution=self.institution,
        )
        other = np.random.default_rng(2).normal(size=128).astype(np.float32)
        other /= np.linalg.norm(other)
        ciphertext, nonce, tag = self.mixin.encrypt_embedding(other)
        UserBiometric.objects.create(
            user=teacher, encrypted_embedding=ciphertext, nonce=nonce, tag=tag, is_active=True,
        )

        matched, _score = self.mixin.match_face(other, institution=self.institution)
        self.assertIsNone(matched)
        self.mixin.ensure_face_not_registered(other, self.institution)
