from __future__ import annotations

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
import zoneinfo
from datetime import datetime
from django.db.models import Sum
from wallet.models import Transaction

class ParentalControlValidatorMixin:
    def validate_parental_controls(self, student, amount, target_date=None):
        from users.models import ParentalControl
        from .models import PreOrder
        try:
            control = student.parental_control
        except (ParentalControl.DoesNotExist, AttributeError):
            return

        today_ec = datetime.now(zoneinfo.ZoneInfo('America/Guayaquil')).date()
        if target_date is None:
            target_date = today_ec

        if control.allowed_days_enabled:
            # 0 = Monday, ..., 6 = Sunday
            if target_date.weekday() not in control.allowed_days:
                raise ValidationError("No está permitido comprar para ese día: está bloqueado por el control parental.")

        if control.daily_limit_enabled:
            preorders_sum = PreOrder.objects.filter(
                student=student,
                daily_menu__date=target_date,
            ).exclude(
                status=PreOrder.Status.CANCELED
            ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

            if target_date == today_ec:
                direct_sum = Transaction.objects.filter(
                    wallet=student.wallet,
                    type=Transaction.Type.CONSUMPTION,
                    status=Transaction.Status.SUCCESS,
                    created_at__date=today_ec,
                    pre_order__isnull=True,
                ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
            else:
                direct_sum = Decimal('0.00')

            spent_target_date = preorders_sum + direct_sum
            if spent_target_date + Decimal(str(amount)) > control.daily_limit_amount:
                raise ValidationError("La preorden supera el límite de gasto diario configurado para ese día.")


import os
import io
import hashlib
import logging
import threading
from typing import TYPE_CHECKING
from django.conf import settings
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from rest_framework.exceptions import ValidationError

if TYPE_CHECKING:
    import numpy as np  # type hints only; imported lazily at runtime

logger = logging.getLogger(__name__)

YUNET_URL = 'https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx'
SFACE_URL = 'https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx'

_BIOMETRIC_LOCK = threading.Lock()
_FACE_DETECTOR = None
_FACE_RECOGNIZER = None


def _get_biometric_model_paths():
    models_dir = os.path.join(settings.BASE_DIR, 'models')
    yunet_path = os.path.join(models_dir, 'face_detection_yunet_2023mar.onnx')
    sface_path = os.path.join(models_dir, 'face_recognition_sface_2021dec.onnx')

    if not os.path.exists(yunet_path) or not os.path.exists(sface_path):
        raise FileNotFoundError(
            "Modelos biométricos no encontrados. Ejecuta 'python manage.py download_biometric_models' para descargarlos."
        )

    return yunet_path, sface_path


def _get_biometric_models():
    """
    Returns (detector, recognizer), lazily initialized once per process under a thread lock.
    """
    global _FACE_DETECTOR, _FACE_RECOGNIZER
    if _FACE_DETECTOR is None or _FACE_RECOGNIZER is None:
        import cv2
        yunet_path, sface_path = _get_biometric_model_paths()
        _FACE_DETECTOR = cv2.FaceDetectorYN.create(
            model=yunet_path,
            config='',
            input_size=(320, 320),
            score_threshold=0.5,
            nms_threshold=0.3,
            top_k=5000,
        )
        _FACE_RECOGNIZER = cv2.FaceRecognizerSF.create(model=sface_path, config='')
    return _FACE_DETECTOR, _FACE_RECOGNIZER


class BiometricValidationMixin:
    """
    Encapsulates all mathematical facial comparison and AES-256-GCM encryption/decryption.
    Guarantees strict compliance with Data Protection Laws:
    - Detects and crops only the facial bounding box, eliminating background room bias.
    - Destroys raw image frames in volatile memory immediately after embedding extraction (LOPDP).
    - Encrypts deep 128-dimensional mathematical vectors with AES-256-GCM.
    - Performs similarity matching in memory without persisting pictures.
    """

    def _get_encryption_key(self) -> bytes:
        """Derives a deterministic 256-bit (32 bytes) key from the biometric seed."""
        # Dedicated seed; falls back to SECRET_KEY for setups not migrated yet.
        seed = settings.BIOMETRIC_ENCRYPTION_KEY or settings.SECRET_KEY
        return hashlib.sha256(seed.encode('utf-8')).digest()

    def encrypt_embedding(self, vector: np.ndarray) -> tuple[bytes, bytes, bytes]:
        """Encrypts an embedding vector using AES-256-GCM."""
        import numpy as np  # lazy: keeps numpy out of the web process until needed
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
        import numpy as np  # lazy: keeps numpy out of the web process until needed
        key = self._get_encryption_key()
        aesgcm = AESGCM(key)
        full_ciphertext = bytes(ciphertext) + bytes(tag)
        raw_bytes = aesgcm.decrypt(bytes(nonce), full_ciphertext, None)
        return np.frombuffer(raw_bytes, dtype=np.float32)

    def extract_face_embedding(self, image_file_or_bytes) -> np.ndarray:
        """
        Detects the face with YuNet, aligns it using 5 facial landmarks,
        and extracts a 128-dimensional Deep SFace feature vector.
        La imagen se descarta de memoria al soltar las referencias (requisito LOPDP).
        """
        import cv2
        import numpy as np
        try:
            if hasattr(image_file_or_bytes, 'read'):
                raw_bytes = image_file_or_bytes.read()
            else:
                raw_bytes = bytes(image_file_or_bytes)

            if not raw_bytes:
                raise ValidationError("No se proveyeron datos de imagen para la identificación biométrica.")

            nparr = np.frombuffer(raw_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None:
                raise ValidationError("Formato de imagen no soportado o archivo corrupto.")

            h, w = img.shape[:2]

            with _BIOMETRIC_LOCK:
                detector, recognizer = _get_biometric_models()
                detector.setInputSize((w, h))
                _, faces = detector.detect(img)

                if faces is None or len(faces) == 0:
                    raise ValidationError(
                        "No se detectó un rostro claro en la imagen. "
                        "Por favor, asegúrate de colocar el rostro dentro del encuadre con buena iluminación."
                    )

                # Pick largest face (the primary subject in the frame)
                best_face = max(faces, key=lambda f: f[2] * f[3])
                aligned_face = recognizer.alignCrop(img, best_face)
                embedding = recognizer.feature(aligned_face).flatten()

            # L2 Normalize
            norm = np.linalg.norm(embedding)
            if norm > 0:
                embedding = embedding / norm

            return embedding
        finally:
            if 'raw_bytes' in locals():
                del raw_bytes
            if 'nparr' in locals():
                del nparr
            if 'img' in locals():
                del img
            if 'aligned_face' in locals():
                del aligned_face

    def calculate_similarity(self, vector_a: np.ndarray, vector_b: np.ndarray) -> float:
        """Calculates cosine similarity between two 128-d deep face embeddings."""
        import numpy as np  # lazy: keeps numpy out of the web process until needed
        norm_a = np.linalg.norm(vector_a)
        norm_b = np.linalg.norm(vector_b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(vector_a.flatten(), vector_b.flatten()) / (norm_a * norm_b))

    def match_face(
        self,
        candidate_embedding: np.ndarray,
        institution=None,
        threshold: float = 0.40,
        all_institutions: bool = False,
    ):
        """
        Matches a candidate embedding against active registered biometric embeddings in memory.
        Only students (user__student_profile__institution) and teachers (user__institution)
        have biometrics, and they are filtered strictly by `institution` (RNF-04).
        Matching across every institution requires the explicit all_institutions=True
        (admin consolidated view); a missing institution never widens the search.
        Performs vectorized cosine comparison with NumPy.
        Returns (matched_user, similarity_score) or (None, -1.0).
        """
        import numpy as np
        from django.db.models import Q
        from users.models import CustomUser, UserBiometric

        if institution is None and not all_institutions:
            raise ValueError('An institution is required to match a face.')

        qs = UserBiometric.objects.filter(
            is_active=True,
            user__role__in=[CustomUser.Role.STUDENT, CustomUser.Role.TEACHER],
        )
        if institution is not None:
            qs = qs.filter(
                Q(user__student_profile__institution=institution) | Q(user__institution=institution)
            )

        biometrics = list(
            qs.select_related('user').only(
                'id',
                'user_id',
                'encrypted_embedding',
                'nonce',
                'tag',
                'user__id',
                'user__username',
                'user__first_name',
                'user__last_name',
                'user__role',
            )
        )

        stored_vectors = []
        valid_users = []
        for bio in biometrics:
            try:
                stored_vector = self.decrypt_embedding(bio.encrypted_embedding, bio.nonce, bio.tag)
                stored_vectors.append(stored_vector)
                valid_users.append(bio.user)
            except Exception as e:
                logger.warning("[BIOMETRIC] Decrypt error for user %s: %s", bio.user_id, e)
                continue

        best_user = None
        best_score = -1.0

        if stored_vectors:
            matrix = np.array(stored_vectors, dtype=np.float32)  # shape (N, 128)
            cand = candidate_embedding.flatten().astype(np.float32)
            cand_norm = np.linalg.norm(cand)
            matrix_norms = np.linalg.norm(matrix, axis=1)
            denom = matrix_norms * cand_norm
            denom = np.where(denom == 0, 1e-9, denom)
            similarities = np.dot(matrix, cand) / denom

            best_idx = int(np.argmax(similarities))
            score = float(similarities[best_idx])
            best_score = score
            if score >= threshold:
                best_user = valid_users[best_idx]

        logger.debug("[BIOMETRIC] Face match evaluation completed: match_found=%s", best_user is not None)
        return best_user, best_score

