"""Services used by the superadmin: audit log, password resets for every role, role
changes and sede suspension. Kept apart from users/services.py (the API services) so
neither grows past what it is responsible for."""
import logging

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from kombu.exceptions import OperationalError

from .audit import display_name, record_audit
from .models import AuditLog, CustomUser
from .services import generate_temporary_password, revoke_user_tokens
from .tasks import send_password_reset_by_admin_email

logger = logging.getLogger(__name__)

Role = CustomUser.Role

# Roles that have a sede and no profile table of their own.
STAFF_ROLES = (Role.ADMIN, Role.OPERATIONS_STAFF, Role.TEACHER)


# ------------------------------------------------- password reset by superadmin
def password_reset_destination(user):
    """(recipient e-mail, login the person types) for the temporary password of `user`,
    or (None, None) when it cannot be delivered. A student has no e-mail: it goes to the
    parent, together with the student's username."""
    if user.role == Role.STUDENT:
        profile = getattr(user, 'student_profile', None)
        parent_user = profile.parent.user if profile else None
        if parent_user and parent_user.is_active and parent_user.deleted_at is None and parent_user.email:
            return parent_user.email, user.username
        return None, None
    return (user.email, user.email) if user.email else (None, None)


def notify_password_reset(recipient_email, user, login, temporary_password):
    """Enqueues the e-mail with the temporary password set by the superadmin. Returns
    False when the broker is unreachable (the password is already changed)."""
    try:
        send_password_reset_by_admin_email.delay(
            recipient_email, user.first_name or user.username, display_name(user), login, temporary_password,
        )
    except (OperationalError, OSError):
        logger.exception('Could not enqueue the password reset e-mail for account %s', user.pk)
        return False
    return True


def reset_account_password(user, *, actor, request=None):
    """Gives `user` a new temporary password and mails it. Returns 'sent', 'no_recipient'
    (nothing changed) or 'email_failed' (changed, but the e-mail could not be queued).
    The account must change it at the next login and its sessions are closed."""
    recipient, login = password_reset_destination(user)
    if not recipient:
        return 'no_recipient'
    temporary_password = generate_temporary_password()
    with transaction.atomic():
        user.set_password(temporary_password)
        user.must_change_password = True
        user.save(update_fields=['password', 'must_change_password'])
        revoke_user_tokens(user)
    record_audit(
        AuditLog.Action.PASSWORD_RESET, actor=actor, target=user, request=request,
        detail={'delivered_to': 'padre' if user.role == Role.STUDENT else 'cuenta'},
    )
    return 'sent' if notify_password_reset(recipient, user, login, temporary_password) else 'email_failed'


# ------------------------------------------------------------------- roles
def role_change_error(user, new_role, actor):
    """Why `user` cannot take `new_role`, or None when the change is allowed."""
    if user.role == new_role:
        return None
    if user.role not in STAFF_ROLES or new_role not in STAFF_ROLES:
        return str(_('Los padres y los estudiantes no cambian de rol: tienen un perfil propio.'))
    if user.deleted_at is not None:
        return str(_('Una cuenta dada de baja no cambia de rol.'))
    if user.is_superuser:
        return str(_('Una cuenta de superadministrador no cambia de rol.'))
    if actor is not None and user.pk == actor.pk:
        return str(_('No puedes cambiar tu propio rol.'))
    if user.role == Role.TEACHER and (
        user.purchases.filter(payment_method='CREDIT').exists() or user.credit_statements.exists()
    ):
        return str(_('Este docente tiene consumos a crédito o estados de cuenta: cambiar su rol dejaría '
                     'huecos en los reportes de RR. HH.'))
    return None


@transaction.atomic
def apply_role_change(user, old_role, *, actor, request=None):
    """Side effects of a role change already saved on `user`."""
    if old_role == Role.TEACHER:
        user.allergies.clear()  # the allergies field only applies to teachers
    revoke_user_tokens(user)
    record_audit(
        AuditLog.Action.ROLE_CHANGED, actor=actor, target=user, request=request,
        detail={'from': old_role, 'to': user.role},
    )


# -------------------------------------------------------------------- sedes
@transaction.atomic
def suspend_institution(institution, reason, *, actor, request=None):
    institution.is_active = False
    institution.suspended_at = timezone.now()
    institution.suspension_reason = (reason or '')[:255]
    institution.save(update_fields=['is_active', 'suspended_at', 'suspension_reason'])
    record_audit(
        AuditLog.Action.INSTITUTION_SUSPENDED, actor=actor, target=institution, request=request,
        detail={'reason': institution.suspension_reason},
    )


@transaction.atomic
def reactivate_institution(institution, *, actor, request=None):
    institution.is_active = True
    institution.suspended_at = None
    institution.suspension_reason = ''
    institution.save(update_fields=['is_active', 'suspended_at', 'suspension_reason'])
    record_audit(AuditLog.Action.INSTITUTION_REACTIVATED, actor=actor, target=institution, request=request)
