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
from kombu.exceptions import OperationalError
from rest_framework.exceptions import ValidationError
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from core.biometrics import BiometricValidationMixin

from .audit import display_name, record_audit
from .models import AuditLog, CustomUser, ParentalControl, StudentProfile, UserBiometric
from .tasks import send_child_password_changed_email, send_staff_credentials_email

logger = logging.getLogger(__name__)

QR_TOKEN_TYPE = 'pos_dynamic_qr'

TEMPORARY_PASSWORD_LENGTH = 8
TEMP_PASSWORD_UPPERCASE = ''.join(c for c in string.ascii_uppercase if c not in 'ILO')
TEMP_PASSWORD_LOWERCASE = ''.join(c for c in string.ascii_lowercase if c not in 'ilo')
TEMP_PASSWORD_DIGITS = '23456789'


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


def revoke_user_tokens(user):
    """Blacklists every refresh token of `user`: after a credential change no
    other device can renew its session (its access token expires on its own)."""
    for outstanding in OutstandingToken.objects.filter(user=user, blacklistedtoken__isnull=True):
        BlacklistedToken.objects.get_or_create(token=outstanding)


@transaction.atomic
def rotate_user_tokens(user):
    """Revokes every refresh token of `user` and issues a new one so the device
    that changed the password keeps its session. Returns the new tokens."""
    revoke_user_tokens(user)
    refresh = RefreshToken.for_user(user)
    return {'access': str(refresh.access_token), 'refresh': str(refresh)}


def generate_temporary_password(length=TEMPORARY_PASSWORD_LENGTH):
    """Short, letters-and-digits password that is easy to read and copy: no
    look-alike characters (I l i L o O 0 1) and no symbols, so a double click
    selects all of it. Has at least one uppercase, one lowercase and one digit."""
    pools = (TEMP_PASSWORD_UPPERCASE, TEMP_PASSWORD_LOWERCASE, TEMP_PASSWORD_DIGITS)
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
    if institution is not None and not institution.is_active:
        raise ValidationError({'institution': _('Esta institución está suspendida: no admite cuentas nuevas.')})
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


def notify_staff_credentials(user, temporary_password):
    """Enqueues the email with the temporary password of a new staff account.
    The account is already committed, so an unreachable broker must not turn the
    creation into a 500: returns False so the admin can be warned. The password
    is never logged."""
    try:
        send_staff_credentials_email.delay(
            user.email, user.first_name, str(user.get_role_display()), temporary_password,
        )
    except (OperationalError, OSError):
        logger.exception('Could not enqueue the credentials email for staff account %s', user.pk)
        return False
    return True


def notify_child_password_changed(student):
    """Enqueues the notice to the parent's e-mail after their child's password
    changed. Best effort: the change is already committed, so an unreachable
    broker or a parent without e-mail must not turn it into an error. The notice
    never carries the password."""
    parent = student.parent.user
    if not parent.email:
        return False
    try:
        send_child_password_changed_email.delay(
            parent.email, parent.first_name, display_name(student.user), student.user.username,
        )
    except (OperationalError, OSError):
        logger.exception('Could not enqueue the password change notice for student %s', student.pk)
        return False
    return True


@transaction.atomic
def change_child_password(student, new_password, parent, request=None):
    """The parent sets a new password for their child: the child's sessions are
    closed (the parent keeps theirs), the change is audited and the parent gets an
    e-mail notice once the change is committed."""
    child = student.user
    child.set_password(new_password)
    child.must_change_password = False
    child.save(update_fields=['password', 'must_change_password'])
    revoke_user_tokens(child)
    record_audit(
        AuditLog.Action.PASSWORD_CHANGED, actor=parent, target=child, request=request,
        source=AuditLog.Source.API, detail={'changed_by': 'parent'},
    )
    transaction.on_commit(lambda: notify_child_password_changed(student))


class ChildRemovalBlocked(Exception):
    """The child still has money, orders or a recharge in flight. The view answers
    409 with the reasons in `blockers` so the app can list them."""
    code = 'child_removal_blocked'

    def __init__(self, blockers):
        self.blockers = blockers
        self.message = child_removal_message(blockers)
        super().__init__(self.message)


def child_removal_message(blockers):
    """Spanish sentence with every reason that prevents the removal."""
    reasons = []
    if 'balance' in blockers:
        reasons.append(_('todavía tiene saldo en su billetera ($%(amount)s)') % {'amount': blockers['balance']})
    if 'pending_pre_orders' in blockers:
        reasons.append(_('tiene preórdenes pendientes de entrega'))
    if 'pending_recharges' in blockers:
        reasons.append(_('tiene una recarga en proceso'))
    return _('No se puede dar de baja a tu hijo/a: %(reasons)s.') % {'reasons': '; '.join(str(r) for r in reasons)}


def child_removal_blockers(student, wallet=None):
    """What stops a parent from removing `student`: money in the wallet, pre-orders
    not delivered yet (today or later) and a card recharge started recently that
    has not finished. Empty dict = it can be removed. Pass the (locked) `wallet`
    when deciding inside a transaction."""
    from core.timezone import local_today
    from wallet.models import Transaction, Wallet

    if wallet is None:
        wallet = Wallet.objects.filter(student=student).first()
    blockers = {}
    if wallet is not None and wallet.balance > 0:
        blockers['balance'] = str(wallet.balance)

    pending_orders = student.pre_orders.filter(status='PENDING', daily_menu__date__gte=local_today()).count()
    if pending_orders:
        blockers['pending_pre_orders'] = pending_orders

    if wallet is not None:
        since = timezone.now() - timedelta(minutes=settings.CHILD_REMOVAL_PENDING_RECHARGE_MINUTES)
        pending_recharges = wallet.transactions.filter(
            type=Transaction.Type.RECHARGE, status=Transaction.Status.PENDING, created_at__gte=since,
        ).count()
        if pending_recharges:
            blockers['pending_recharges'] = pending_recharges
    return blockers


@transaction.atomic
def remove_child(student, parent, request=None):
    """The parent removes their child. Soft delete, never a real one: the wallet,
    its transactions and the pre-orders are PROTECTed and the sales history must
    keep the child. The account becomes unusable, the face, allergies, parental
    control and photo are erased and the child's sessions are closed. Raises
    ChildRemovalBlocked while the child still has a balance or open orders."""
    from wallet.models import Wallet

    # Lock the wallet so a debit or credit in flight cannot slip in between the
    # check and the removal.
    wallet = Wallet.objects.select_for_update().filter(student=student).first()
    blockers = child_removal_blockers(student, wallet)
    if blockers:
        raise ChildRemovalBlocked(blockers)

    child = student.user
    soft_delete_account(child)
    student.allergies.clear()
    ParentalControl.objects.filter(student=student).delete()
    _erase_profile_picture(student)
    revoke_user_tokens(child)
    record_audit(
        AuditLog.Action.ACCOUNT_REMOVED, actor=parent, target=child, request=request,
        source=AuditLog.Source.API, detail={'role': child.role, 'removed_by': 'parent'},
    )


def _erase_profile_picture(student):
    """Clears the minor's photo; the file is deleted once the removal commits."""
    picture = student.profile_picture
    if not picture:
        return
    name, storage = picture.name, picture.storage
    student.profile_picture = ''
    student.save(update_fields=['profile_picture'])
    transaction.on_commit(lambda: storage.delete(name))


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


def record_terms_acceptance(user, document, request=None, student=None):
    """Stores the acceptance of the current TERMS_VERSION as legal evidence."""
    from .audit import client_ip
    from .models import TermsAcceptance

    return TermsAcceptance.objects.create(
        user=user, student=student, document=document,
        version=settings.TERMS_VERSION, ip=client_ip(request),
    )
