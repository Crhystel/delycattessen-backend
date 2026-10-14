from rest_framework.permissions import BasePermission
from django.utils.translation import gettext_lazy as _
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
        from .mixins import resolve_institution_scope
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


class IsAdministrator(BasePermission):
    message = _('Solo el Administrador puede realizar esta acción.')

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.role == request.user.Role.ADMIN
        )


class CanRequestPasswordReset(BasePermission):
    """
    Custom Permission Class: delegates authorization to a specialized class.
    Currently always returns True (AllowAny), but establishes an architectural
    contract for the future (e.g. rate limiting, IP blocking, suspended users).
    """
    def has_permission(self, request, view):
        return True

class IsParentUser(BasePermission):
    """Only CustomUser with role PARENT can access the endpoint."""
    message = 'Solo un padre de familia puede registrar estudiantes.'
    def has_permission(self, request, view):
        return bool( request.user and request.user.is_authenticated and request.user.role == CustomUser.Role.PARENT
        )

class CanManageAllergies(BasePermission):
    """A teacher can only manage their own allergies. A parent can only
    manage the allergies of their own children."""

    message = 'No tienes permiso para gestionar las alergias de este usuario.'

    def has_permission(self, request, view):
        target_user_id = request.data.get('target_user_id') or request.query_params.get('target_user_id')
        if not target_user_id:
            return False

        try:
            target_user = CustomUser.objects.get(pk=target_user_id)
        except CustomUser.DoesNotExist:
            return False

        if request.user.role == CustomUser.Role.TEACHER:
            return target_user.id == request.user.id

        if request.user.role == CustomUser.Role.PARENT:
            if not hasattr(request.user, 'parent_profile') or not hasattr(target_user, 'student_profile'):
                return False
            return target_user.student_profile.parent_id == request.user.parent_profile.id

        return False
    
class IsParentOfStudent(BasePermission):
    message = 'No tienes permiso sobre este estudiante'
    def has_permission(self, request, view):
        student_id = view.kwargs.get('student_id')
        if not student_id or request.user.role != CustomUser.Role.PARENT:
            return False
        return StudentProfile.objects.filter(pk=student_id, parent__user=request.user).exists()


class IsOperativeUser(BasePermission):
    """Permission class ensuring only OPERATIONS_STAFF or ADMIN can trigger POS identification."""
    message = _('Solo el personal operativo puede realizar identificaciones en el POS.')

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.role in [CustomUser.Role.OPERATIONS_STAFF, CustomUser.Role.ADMIN]
        )


class IsStudentOrTeacherUser(BasePermission):
    """Permission class for student or teacher QR generation and contingency access."""
    message = _('Acceso restringido a Estudiantes y Docentes.')

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.role in [CustomUser.Role.STUDENT, CustomUser.Role.TEACHER]
        )