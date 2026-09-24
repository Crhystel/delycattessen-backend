import uuid
import jwt
import numpy as np
from datetime import datetime, timezone, timedelta
from django.test import TestCase
from django.conf import settings
from rest_framework.test import APIClient
from rest_framework import status
from users.models import CustomUser, Institution, UserBiometric
from pos.mixins import BiometricValidationMixin

class BiometricEncryptionTests(TestCase, BiometricValidationMixin):
    def test_aes_gcm_encryption_roundtrip(self):
        """Verifies that 128-d float embeddings encrypt and decrypt losslessly."""
        original_vector = np.random.randn(128).astype(np.float32)
        ciphertext, nonce, tag = self.encrypt_embedding(original_vector)

        # Ciphertext must differ from raw vector
        self.assertNotEqual(ciphertext, original_vector.tobytes())
        self.assertEqual(len(nonce), 12)
        self.assertEqual(len(tag), 16)

        decrypted_vector = self.decrypt_embedding(ciphertext, nonce, tag)
        np.testing.assert_almost_equal(original_vector, decrypted_vector, decimal=5)

    def test_cosine_similarity_identical_vectors(self):
        """Identical vectors must have similarity close to 1.0."""
        vec = np.random.randn(128).astype(np.float32)
        sim = self.calculate_similarity(vec, vec)
        self.assertAlmostEqual(sim, 1.0, places=4)


class DynamicQRIdentificationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.inst = Institution.objects.create(name="Test Inst")
        self.operator = CustomUser.objects.create_user(
            username="cashier",
            email="cashier@test.com",
            password="password123",
            role=CustomUser.Role.OPERATIONS_STAFF,
            institution=self.inst
        )
        self.student = CustomUser.objects.create_user(
            username="student1",
            email="student1@test.com",
            password="password123",
            role=CustomUser.Role.STUDENT,
            institution=self.inst,
            first_name="Carlitos",
            last_name="Perez"
        )


    def test_dynamic_qr_identification_success(self):
        """Tests identifying student with a valid dynamic QR token."""
        self.client.force_authenticate(user=self.operator)
        nonce = uuid.uuid4().hex
        payload = {
            'sub': str(self.student.id),
            'user_id': self.student.id,
            'username': self.student.username,
            'role': self.student.role,
            'nonce': nonce,
            'type': 'pos_dynamic_qr',
            'iat': datetime.now(timezone.utc),
            'exp': datetime.now(timezone.utc) + timedelta(seconds=60),
        }
        token = jwt.encode(payload, settings.SECRET_KEY, algorithm='HS256')

        response = self.client.post('/api/pos/identify/qr/', {'token': token}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['user_id'], self.student.id)
        self.assertEqual(response.data['full_name'], "Carlitos Perez")
        self.assertEqual(response.data['identification_method'], "DYNAMIC_QR")

    def test_dynamic_qr_single_use_prevents_replay(self):
        """Tests that reusing the same QR nonce is rejected to prevent spoofing."""
        self.client.force_authenticate(user=self.operator)
        nonce = uuid.uuid4().hex
        payload = {
            'sub': str(self.student.id),
            'user_id': self.student.id,
            'username': self.student.username,
            'role': self.student.role,
            'nonce': nonce,
            'type': 'pos_dynamic_qr',
            'iat': datetime.now(timezone.utc),
            'exp': datetime.now(timezone.utc) + timedelta(seconds=60),
        }

        token = jwt.encode(payload, settings.SECRET_KEY, algorithm='HS256')

        # First use -> OK
        res1 = self.client.post('/api/pos/identify/qr/', {'token': token}, format='json')
        self.assertEqual(res1.status_code, status.HTTP_200_OK)

        # Replay attempt -> 400 Bad Request
        res2 = self.client.post('/api/pos/identify/qr/', {'token': token}, format='json')
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("ya fue utilizado", res2.data['detail'])

    def test_non_operator_cannot_access_pos_identify(self):
        """Verifies IsOperativeUser rejects student/parent accounts."""
        self.client.force_authenticate(user=self.student)
        response = self.client.post('/api/pos/identify/qr/', {'token': 'dummy'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
