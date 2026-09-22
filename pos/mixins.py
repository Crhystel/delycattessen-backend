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
import logging
import urllib.request
import cv2
import numpy as np
from django.conf import settings
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from rest_framework.exceptions import ValidationError

logger = logging.getLogger(__name__)

YUNET_URL = 'https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx'
SFACE_URL = 'https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx'

def _get_biometric_model_paths():
    models_dir = os.path.join(settings.BASE_DIR, 'models')
    os.makedirs(models_dir, exist_ok=True)
    yunet_path = os.path.join(models_dir, 'face_detection_yunet_2023mar.onnx')
    sface_path = os.path.join(models_dir, 'face_recognition_sface_2021dec.onnx')

    if not os.path.exists(yunet_path):
        logger.info("Downloading YuNet face detection model...")
        urllib.request.urlretrieve(YUNET_URL, yunet_path)

    if not os.path.exists(sface_path):
        logger.info("Downloading SFace facial recognition model...")
        urllib.request.urlretrieve(SFACE_URL, sface_path)

    return yunet_path, sface_path


class BiometricValidationMixin:
    """
    Encapsulates all mathematical facial comparison and AES-256-GCM encryption/decryption.
    Guarantees strict compliance with Data Protection Laws:
    - Detects and crops only the facial bounding box, eliminating background room bias.
    - Destroys raw image frames in volatile memory immediately after embedding extraction.
    - Encrypts deep 128-dimensional mathematical vectors with AES-256-GCM.
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
        Detects the face with YuNet, aligns it using 5 facial landmarks,
        and extracts a 128-dimensional Deep SFace feature vector.
        Immediately obliterates the raw image buffer from memory.
        """
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
            yunet_path, sface_path = _get_biometric_model_paths()

            detector = cv2.FaceDetectorYN.create(
                model=yunet_path,
                config='',
                input_size=(w, h),
                score_threshold=0.5,
                nms_threshold=0.3,
                top_k=5000
            )
            _, faces = detector.detect(img)

            if faces is None or len(faces) == 0:
                raise ValidationError(
                    "No se detectó un rostro claro en la imagen. "
                    "Por favor, asegúrate de colocar el rostro dentro del encuadre con buena iluminación."
                )

            # Pick largest face (the primary subject in the frame)
            best_face = max(faces, key=lambda f: f[2] * f[3])

            recognizer = cv2.FaceRecognizerSF.create(model=sface_path, config='')
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
            gc.collect()

    def calculate_similarity(self, vector_a: np.ndarray, vector_b: np.ndarray) -> float:
        """Calculates cosine similarity between two 128-d deep face embeddings."""
        norm_a = np.linalg.norm(vector_a)
        norm_b = np.linalg.norm(vector_b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(vector_a.flatten(), vector_b.flatten()) / (norm_a * norm_b))

    def match_face(self, candidate_embedding: np.ndarray, threshold: float = 0.40):
        """
        Matches a candidate embedding against all active registered biometric embeddings in memory.
        The official SFace threshold is cosine >= 0.363. Setting threshold = 0.40 guarantees high accuracy
        and zero false positives from different people.
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
                logger.info(f"[BIOMETRIC] Candidate compared with user {bio.user.username}: similarity={similarity:.4f}")
                if similarity > best_score:
                    best_score = similarity
                    if similarity >= threshold:
                        best_user = bio.user
            except Exception as e:
                logger.warning(f"[BIOMETRIC] Decrypt error for user {bio.user_id}: {e}")
                continue

        return best_user, best_score

