from rest_framework.permissions import BasePermission, SAFE_METHODS
from django.core.cache import cache
from django.utils.translation import gettext_lazy as _
from .mixins import CODE_TTL_SECONDS
from .models import CustomUser, StudentProfile


def get_object_institution_id(obj):
    """Institution id an object belongs to, resolved through its student when
    it has no direct institution (users, student profiles, pre-orders)."""
    student = getattr(obj, 'student', None) or getattr(obj, 'student_profile', None)
    if isinstance(obj, StudentProfile):
        return obj.institution_id
    if student is not None:
        return student.institution_id
    return getattr(obj, 'institution_id', None)


class SameInstitutionPermission(BasePermission):
    """Object-level: the object must belong to the caller's institution. An
    ADMIN is allowed any object here because the queryset already narrows it
    (own sede by default, wider only when ?institution= is requested)."""
    message = _('No tiene acceso a la información de esta institución.')

    def has_object_permission(self, request, view, obj):
        user = request.user
        if user.role == user.Role.ADMIN:
            return True
        return get_object_institution_id(obj) == user.institution_id


class SameInstitutionAsTarget(BasePermission):
    """Object-level for POS operators: the identified person (user, student
    profile or pre-order) must belong to the operator's institution. An admin
    consolidating with ?institution=all may act on any sede."""
    message = _('Esta persona pertenece a otra institución.')

    def has_object_permission(self, request, view, obj):
        from core.mixins import resolve_institution_scope
        operator_institution = resolve_institution_scope(request)
        if operator_institution is None:
            return True
        return get_object_institution_id(obj) == operator_institution.id


class CanRegisterBiometric(BasePermission):
    """Face biometrics exist only for students and teachers. A parent
    registers their own children; an admin registers teachers of their own
    institution. Nobody else (staff, other roles) can register biometrics."""
    message = _('No tiene permiso para registrar la biometría de este usuario.')

    def has_object_permission(self, request, view, obj):
        user = request.user
        if obj.role == CustomUser.Role.STUDENT:
            profile = getattr(obj, 'student_profile', None)
            return bool(
                user.role == CustomUser.Role.PARENT
                and profile is not None
                and profile.parent.user_id == user.id
            )
        if obj.role == CustomUser.Role.TEACHER:
            return bool(
                user.role == CustomUser.Role.ADMIN
                and user.institution_id is not None
                and obj.institution_id == user.institution_id
            )
        return False


class RolePermission(BasePermission):
    """Base for "only these roles" permissions: subclasses set `allowed_roles`
    (and `message`) and nothing else."""
    allowed_roles = ()

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.role in self.allowed_roles
        )


class IsAdministrator(RolePermission):
    message = _('Solo el Administrador puede realizar esta acción.')
    allowed_roles = (CustomUser.Role.ADMIN,)


class CanRequestPasswordReset(BasePermission):
    """Public endpoint guard: at most MAX_REQUESTS calls per email and view
    inside the reset-code lifetime, so codes cannot be brute-forced or the
    mailbox flooded."""
    message = _('Demasiados intentos. Intente nuevamente más tarde.')
    MAX_REQUESTS = 5

    def has_permission(self, request, view):
        email = str(request.data.get('email', '')).lower()
        if not email:
            return True
        key = f'password_reset_calls:{view.__class__.__name__}:{email}'
        cache.add(key, 0, timeout=CODE_TTL_SECONDS)
        return cache.incr(key) <= self.MAX_REQUESTS


class IsParentUser(RolePermission):
    """Only CustomUser with role PARENT can access the endpoint."""
    message = _('Solo un padre de familia puede realizar esta acción.')
    allowed_roles = (CustomUser.Role.PARENT,)


class IsTeacherUser(RolePermission):
    """Only CustomUser with role TEACHER can access the endpoint."""
    message = _('Solo un docente puede realizar esta acción.')
    allowed_roles = (CustomUser.Role.TEACHER,)


class IsParentOfStudent(BasePermission):
    message = _('No tienes permiso sobre este estudiante')

    def has_permission(self, request, view):
        student_id = view.kwargs.get('student_id')
        if not student_id or request.user.role != CustomUser.Role.PARENT:
            return False
        return StudentProfile.objects.filter(pk=student_id, parent__user=request.user).exists()


class IsOperativeUser(RolePermission):
    """Only OPERATIONS_STAFF or ADMIN can trigger POS identification."""
    message = _('Solo el personal operativo puede realizar identificaciones en el POS.')
    allowed_roles = (CustomUser.Role.OPERATIONS_STAFF, CustomUser.Role.ADMIN)


class IsStudentOrTeacherUser(RolePermission):
    """Student or teacher QR generation and contingency access."""
    message = _('Acceso restringido a Estudiantes y Docentes.')
    allowed_roles = (CustomUser.Role.STUDENT, CustomUser.Role.TEACHER)


class CanManageStaffAccount(BasePermission):
    """Object-level policy for what an admin may do to a staff account.
    - Operations staff: full management (edit, activate/deactivate, delete).
    - Teacher: the teacher owns the profile. The admin may only (de)activate the
      account, except while it is still pending activation (must_change_password
      is True): nothing to protect yet, so the admin may also correct its data
      or remove it."""
    message = _('El perfil de un docente lo gestiona el propio docente. Solo puede activar o desactivar su cuenta.')

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS or obj.role != CustomUser.Role.TEACHER:
            return True
        if obj.must_change_password:
            return True
        if request.method == 'DELETE':
            self.message = _('Un docente activo gestiona su propia cuenta. Puede desactivarla, no eliminarla.')
            return False
        return set(request.data.keys()) <= {'is_active'}
