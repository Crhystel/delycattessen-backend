import hashlib
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.biometrics import BiometricValidationMixin
from users.models import UserBiometric


class _PreviousSeedCipher(BiometricValidationMixin):
    """Reads embeddings with the previous seed (the mixin's default is the new one)."""

    def __init__(self, seed: str):
        self._seed = seed

    def _get_encryption_key(self) -> bytes:
        return hashlib.sha256(self._seed.encode('utf-8')).digest()


class Command(BaseCommand):
    help = (
        "Re-cifra los vectores faciales guardados: descifra con OLD_BIOMETRIC_SEED "
        "y vuelve a cifrar con la clave biométrica actual."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Solo verifica que todos los registros se puedan descifrar; no escribe nada.',
        )

    def handle(self, *args, **options):
        old_seed = os.environ.get('OLD_BIOMETRIC_SEED')
        if not old_seed:
            raise CommandError('Define la variable de entorno OLD_BIOMETRIC_SEED con la clave anterior.')

        old_cipher = _PreviousSeedCipher(old_seed)
        new_cipher = BiometricValidationMixin()
        if old_cipher._get_encryption_key() == new_cipher._get_encryption_key():
            raise CommandError('La clave anterior y la actual son iguales; no hay nada que rotar.')

        # Convert everything in memory first: if one row fails, nothing is written.
        converted = []
        for biometric in UserBiometric.objects.all():
            try:
                vector = old_cipher.decrypt_embedding(
                    biometric.encrypted_embedding, biometric.nonce, biometric.tag
                )
            except Exception:
                raise CommandError(
                    f'No se pudo descifrar el registro del usuario {biometric.user_id} '
                    'con la clave anterior. No se modificó nada.'
                )
            converted.append((biometric, *new_cipher.encrypt_embedding(vector)))

        if options['dry_run']:
            self.stdout.write(f'Dry run: {len(converted)} registro(s) se pueden rotar. No se escribió nada.')
            return

        with transaction.atomic():
            for biometric, ciphertext, nonce, tag in converted:
                biometric.encrypted_embedding = ciphertext
                biometric.nonce = nonce
                biometric.tag = tag
                biometric.save(update_fields=['encrypted_embedding', 'nonce', 'tag', 'updated_at'])

        self.stdout.write(self.style.SUCCESS(f'{len(converted)} registro(s) re-cifrados con la clave nueva.'))
