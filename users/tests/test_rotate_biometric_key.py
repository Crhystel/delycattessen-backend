import os
from unittest import mock

import numpy as np
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from pos.mixins import BiometricValidationMixin
from users.management.commands.rotate_biometric_key import _PreviousSeedCipher
from users.models import CustomUser, Institution, UserBiometric

OLD_SEED = 'old-seed-for-tests'
NEW_SEED = 'new-seed-for-tests'


def _cipher_for(seed):
    """Mixin whose AES key comes from an explicit seed."""
    return _PreviousSeedCipher(seed)


@override_settings(BIOMETRIC_ENCRYPTION_KEY=NEW_SEED)
class RotateBiometricKeyTests(TestCase):
    def setUp(self):
        institution = Institution.objects.create(name="Martim Cereré")
        self.user = CustomUser.objects.create_user(
            username="student_a", email="student_a@delycattessen.com",
            password="test1234", role=CustomUser.Role.STUDENT, institution=institution,
        )
        self.vector = np.random.randn(128).astype(np.float32)
        ciphertext, nonce, tag = _cipher_for(OLD_SEED).encrypt_embedding(self.vector)
        self.biometric = UserBiometric.objects.create(
            user=self.user, encrypted_embedding=ciphertext, nonce=nonce, tag=tag,
        )

    def _rotate(self, *args, seed=OLD_SEED):
        with mock.patch.dict('os.environ', {'OLD_BIOMETRIC_SEED': seed}):
            call_command('rotate_biometric_key', *args)

    def test_rotation_reencrypts_with_the_new_key(self):
        self._rotate()
        self.biometric.refresh_from_db()
        decrypted = BiometricValidationMixin().decrypt_embedding(
            self.biometric.encrypted_embedding, self.biometric.nonce, self.biometric.tag
        )
        np.testing.assert_almost_equal(decrypted, self.vector, decimal=5)
        with self.assertRaises(Exception):
            _cipher_for(OLD_SEED).decrypt_embedding(
                self.biometric.encrypted_embedding, self.biometric.nonce, self.biometric.tag
            )

    def test_dry_run_writes_nothing(self):
        before = bytes(self.biometric.encrypted_embedding)
        self._rotate('--dry-run')
        self.biometric.refresh_from_db()
        self.assertEqual(bytes(self.biometric.encrypted_embedding), before)

    def test_wrong_old_seed_changes_nothing(self):
        before = bytes(self.biometric.encrypted_embedding)
        with self.assertRaises(CommandError):
            self._rotate(seed='some-other-seed')
        self.biometric.refresh_from_db()
        self.assertEqual(bytes(self.biometric.encrypted_embedding), before)

    def test_missing_old_seed_is_rejected(self):
        with mock.patch.dict('os.environ'):
            os.environ.pop('OLD_BIOMETRIC_SEED', None)
            with self.assertRaises(CommandError):
                call_command('rotate_biometric_key')

    def test_same_seed_is_rejected(self):
        with self.assertRaises(CommandError):
            self._rotate(seed=NEW_SEED)
