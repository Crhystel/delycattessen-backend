import logging
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone

import jwt
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

from core.biometrics import BiometricValidationMixin

from .models import CustomUser, StudentProfile, UserBiometric

logger = logging.getLogger(__name__)

QR_TOKEN_TYPE = 'pos_dynamic_qr'


@transaction.atomic
def soft_delete_account(user):
    """
    Soft delete shared by every role. The row stays so the Sale / Transaction
    history (PROTECT foreign keys) and the reports keep the person's name, but
    the account can no longer be used and its personal data is erased:
    - email and username are freed (the address can be registered again),
    - the password becomes unusable,
    - facial biometrics and allergies are deleted.
    Names, role and institution are kept on purpose to audit reports.
    """
    user.is_active = False
    user.deleted_at = timezone.now()
    user.email = None
    user.username = f'deleted-{user.pk}'
    user.must_change_password = False
    user.set_unusable_password()
    user.save(update_fields=[
        'is_active', 'deleted_at', 'email', 'username', 'must_change_password', 'password',
    ])
    UserBiometric.objects.filter(user=user).delete()
    user.allergies.clear()
    return user


def generate_temporary_password(length=12):
    pools = (string.ascii_uppercase, string.ascii_lowercase, string.digits, '!@#$%^&*')
    chars = [secrets.choice(pool) for pool in pools]
    alphabet = ''.join(pools)
    chars += [secrets.choice(alphabet) for _idx in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return ''.join(chars)


@transaction.atomic
def create_staff_account(validated_data, admin, target_institution=None):
    """
    Creates a teacher or operations-staff account with a temporary password the
    user must change on first login. Returns (user, temporary_password).
    Institution priority: explicit one in the data, the write target resolved by
    the view (?institution=<id>), then the admin's own sede.
    """
    data = dict(validated_data)
    data.pop('is_active', None)
    institution = data.pop('institution', None) or target_institution or admin.institution
    temporary_password = generate_temporary_password()
    user = CustomUser.objects.create_user(
        username=data['email'],
        email=data['email'],
        password=temporary_password,
        first_name=data['first_name'],
        second_name=data.get('second_name', ''),
        last_name=data['last_name'],
        second_last_name=data.get('second_last_name', ''),
        role=data['role'],
        institution=institution,
        must_change_password=True,
    )
    return user, temporary_password


def extract_embedding(photo):
    """Face vector of a photo. Raises ValidationError when no usable face is found."""
    photo.seek(0)
    try:
        return BiometricValidationMixin().extract_face_embedding(photo)
    except ValidationError:
        raise
    except Exception:
        logger.exception('[BIOMETRIC] Could not extract the face embedding')
        raise ValidationError(_('No se pudo procesar la fotografía para el reconocimiento facial.'))


def extract_unique_embedding(photo, institution, exclude_user=None):
    """Face vector of a photo that no other person of the institution already has
    (FaceAlreadyRegistered, 409). `exclude_user` is the owner when changing a photo."""
    vector = extract_embedding(photo)
    BiometricValidationMixin().ensure_face_not_registered(vector, institution, exclude_user=exclude_user)
    return vector


def store_embedding(user, vector):
    """Saves the vector encrypted (AES-256-GCM); the picture itself is never stored here."""
    ciphertext, nonce, tag = BiometricValidationMixin().encrypt_embedding(vector)
    UserBiometric.objects.update_or_create(
        user=user,
        defaults={'encrypted_embedding': ciphertext, 'nonce': nonce, 'tag': tag, 'is_active': True},
    )


def register_biometric(user, photo, institution):
    """Registers or replaces the facial biometric of `user` from a photo."""
    vector = extract_unique_embedding(photo, institution, exclude_user=user)
    store_embedding(user, vector)


@transaction.atomic
def register_student(parent_profile, validated_data):
    """
    Creates a student (user, profile with photo and wallet) under a parent. The
    face is extracted first so a face already registered in the institution
    rejects the registration (409) before anything is created. A photo without
    a usable face still registers the student, just without biometrics.
    Returns the StudentProfile with `biometric_registered` set.
    """
    from wallet.models import Wallet

    institution = validated_data['institution']
    photo = validated_data['profile_picture']

    vector = None
    try:
        vector = extract_embedding(photo)
    except ValidationError as exc:
        logger.warning('[BIOMETRIC] Student registered without biometrics: %s', exc.detail)
    if vector is not None:
        BiometricValidationMixin().ensure_face_not_registered(vector, institution)

    student_user = CustomUser(
        username=validated_data['username'],
        email=None,
        first_name=validated_data['first_name'],
        second_name=validated_data.get('second_name', ''),
        last_name=validated_data['first_last_name'],
        second_last_name=validated_data.get('second_last_name', ''),
        role=CustomUser.Role.STUDENT,
    )
    student_user.set_password(validated_data['password'])
    student_user.save()

    photo.seek(0)
    student_profile = StudentProfile.objects.create(
        user=student_user, institution=institution, profile_picture=photo, parent=parent_profile,
    )

    student_profile.biometric_registered = False
    if vector is not None:
        try:
            store_embedding(student_user, vector)
            student_profile.biometric_registered = True
        except Exception:
            logger.exception('[BIOMETRIC] Could not save the biometrics of the new student')

    Wallet.objects.create(student=student_profile, low_balance_threshold=settings.WALLET_LOW_BALANCE_DEFAULT)
    return student_profile


class QrTokenError(Exception):
    """Base for a QR token that cannot be used."""


class QrTokenExpired(QrTokenError):
    pass


class QrTokenInvalid(QrTokenError):
    pass


def build_qr_token(user):
    """Short-lived, single-use (nonce) JWT a student or teacher shows at the POS.
    Returns (token, issued_at, ttl_seconds)."""
    now = datetime.now(dt_timezone.utc)
    ttl = settings.QR_TOKEN_TTL_SECONDS
    payload = {
        'sub': str(user.id),
        'user_id': user.id,
        'username': user.username,
        'role': user.role,
        'nonce': uuid.uuid4().hex,
        'type': QR_TOKEN_TYPE,
        'iat': now,
        'exp': now + timedelta(seconds=ttl),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm='HS256'), now, ttl


def decode_qr_token(token):
    """Verified payload of a QR token. Raises QrTokenExpired / QrTokenInvalid."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=['HS256'])
    except jwt.ExpiredSignatureError as exc:
        raise QrTokenExpired from exc
    except jwt.InvalidTokenError as exc:
        raise QrTokenInvalid from exc
    if payload.get('type') != QR_TOKEN_TYPE:
        raise QrTokenInvalid
    return payload


def wallet_balance_of(user):
    """Balance of the wallet a student owns or a teacher uses; "0.00" without one."""
    if hasattr(user, 'student_profile') and hasattr(user.student_profile, 'wallet'):
        return str(user.student_profile.wallet.balance)
    if hasattr(user, 'wallet'):
        return str(user.wallet.balance)
    return '0.00'
