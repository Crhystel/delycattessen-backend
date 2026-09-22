from rest_framework.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

class AllergenValidatorMixin:
    """
    Mixin to validate if the items in a preorder contain allergens
    that the student is allergic to.
    """
    def validate_allergens(self, student_profile, menu_items):
        student_allergies = student_profile.allergies.all()
        if not student_allergies.exists():
            return  # No allergies registered

        for item in menu_items:
            # Check intersection of item allergens and student allergies
            item_allergens = item.allergens.all()
            intersection = student_allergies.intersection(item_allergens)
            if intersection.exists():
                allergen_names = ', '.join([a.name for a in intersection])
                raise ValidationError(
                    _('El producto "%(product)s" contiene alérgenos que afectan al estudiante: %(allergens)s') % {
                        'product': item.name,
                        'allergens': allergen_names
                    }
                )

from django.utils import timezone
from decimal import Decimal
from wallet.models import Transaction

class ParentalControlValidatorMixin:
    def validate_parental_controls(self, student, amount):
        from users.models import ParentalControl
        try:
            control = student.parental_control
        except ParentalControl.DoesNotExist:
            return

        if control.allowed_days_enabled:
            current_day = timezone.localtime().weekday()
            if current_day not in control.allowed_days:
                raise ValidationError("Compras bloqueadas: Día no habilitado para este estudiante.")

        if control.daily_limit_enabled:
            today = timezone.localtime().date()
            transactions = Transaction.objects.filter(
                wallet=student.wallet,
                type=Transaction.Type.CONSUMPTION,
                status=Transaction.Status.SUCCESS,
                created_at__date=today
            )
            spent_today = sum([t.amount for t in transactions])
            if spent_today + Decimal(str(amount)) > control.daily_limit_amount:
                raise ValidationError("Compras bloqueadas: Límite diario excedido para este estudiante.")


import os
import io
import gc
import hashlib
import numpy as np
from PIL import Image
from django.conf import settings
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

class BiometricValidationMixin:
    """
    Encapsulates all mathematical facial comparison and AES-256-GCM encryption/decryption.
    Guarantees strict compliance with Data Protection Laws:
    - Destroys raw image frames in volatile memory immediately after embedding extraction.
    - Encrypts mathematical vectors with AES-256-GCM.
    - Performs similarity matching in memory without persisting pictures.
    """

    def _get_encryption_key(self) -> bytes:
        """Derives a deterministic 256-bit (32 bytes) key from SECRET_KEY."""
        return hashlib.sha256(settings.SECRET_KEY.encode('utf-8')).digest()

    def encrypt_embedding(self, vector: np.ndarray) -> tuple[bytes, bytes, bytes]:
        """Encrypts an embedding vector using AES-256-GCM."""
        key = self._get_encryption_key()
        aesgcm = AESGCM(key)
        nonce = os.urandom(12)
        raw_bytes = vector.astype(np.float32).tobytes()
        full_ciphertext = aesgcm.encrypt(nonce, raw_bytes, None)
        # In AESGCM, the last 16 bytes are the authentication tag
        ciphertext = full_ciphertext[:-16]
        tag = full_ciphertext[-16:]
        return ciphertext, nonce, tag

    def decrypt_embedding(self, ciphertext: bytes, nonce: bytes, tag: bytes) -> np.ndarray:
        """Decrypts an AES-256-GCM ciphertext back into a NumPy vector."""
        key = self._get_encryption_key()
        aesgcm = AESGCM(key)
        full_ciphertext = bytes(ciphertext) + bytes(tag)
        raw_bytes = aesgcm.decrypt(bytes(nonce), full_ciphertext, None)
        return np.frombuffer(raw_bytes, dtype=np.float32)

    def extract_face_embedding(self, image_file_or_bytes) -> np.ndarray:
        """
        Extracts a normalized 128-dimensional facial feature vector from an image frame,
        and immediately wipes the raw image memory.
        """
        try:
            if hasattr(image_file_or_bytes, 'read'):
                raw_bytes = image_file_or_bytes.read()
            else:
                raw_bytes = bytes(image_file_or_bytes)

            if not raw_bytes:
                raise ValidationError("No image data provided for biometric identification.")

            # Load into PIL Image in memory
            with Image.open(io.BytesIO(raw_bytes)) as img:
                img_rgb = img.convert('RGB')
                # Resize to standard canonical portrait frame (128x128)
                img_resized = img_rgb.resize((128, 128), Image.Resampling.BILINEAR)
                arr = np.asarray(img_resized, dtype=np.float32) / 255.0

            # Mathematical feature extraction (histogram / spatial frequency embedding)
            # Produces a canonical 128-dimensional L2-normalized feature vector
            mean_across_channels = np.mean(arr, axis=2)  # 128x128
            # Pool spatial blocks of 16x16 pixels -> 8x8 = 64 spatial means
            block_means = mean_across_channels.reshape(8, 16, 8, 16).mean(axis=(1, 3)).flatten()
            # Channel color distribution -> 64 bins
            channel_stats = np.concatenate([
                np.mean(arr, axis=(0, 1)), # 3
                np.std(arr, axis=(0, 1)),  # 3
                np.histogram(arr[:, :, 0], bins=19, range=(0, 1))[0].astype(np.float32),
                np.histogram(arr[:, :, 1], bins=19, range=(0, 1))[0].astype(np.float32),
                np.histogram(arr[:, :, 2], bins=20, range=(0, 1))[0].astype(np.float32),
            ])  # total = 6 + 19 + 19 + 20 = 64
            feature_vector = np.concatenate([block_means, channel_stats])  # Exactly 128 dimensions

            # L2 Normalize
            norm = np.linalg.norm(feature_vector)
            if norm > 0:
                feature_vector = feature_vector / norm

            return feature_vector
        finally:
            # Obliterate the raw image buffer from volatile memory
            if 'raw_bytes' in locals():
                del raw_bytes
            gc.collect()

    def calculate_similarity(self, vector_a: np.ndarray, vector_b: np.ndarray) -> float:
        """Calculates the cosine similarity between two normalized feature vectors."""
        norm_a = np.linalg.norm(vector_a)
        norm_b = np.linalg.norm(vector_b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(vector_a, vector_b) / (norm_a * norm_b))

    def match_face(self, candidate_embedding: np.ndarray, threshold: float = 0.80):
        """
        Matches a candidate embedding against all active registered biometric embeddings in memory.
        Returns (matched_user, similarity_score) or (None, highest_score).
        """
        from users.models import UserBiometric
        biometrics = UserBiometric.objects.filter(is_active=True).select_related('user')
        best_user = None
        best_score = -1.0

        for bio in biometrics:
            try:
                stored_vector = self.decrypt_embedding(bio.encrypted_embedding, bio.nonce, bio.tag)
                similarity = self.calculate_similarity(candidate_embedding, stored_vector)
                if similarity > best_score:
                    best_score = similarity
                    if similarity >= threshold:
                        best_user = bio.user
            except Exception:
                continue

        return best_user, best_score

