from rest_framework.permissions import BasePermission
from django.utils.translation import gettext_lazy as _
from .models import CustomUser


class SameInstitutionPermission(BasePermission):
    message = _('No tiene acceso a la información de esta institución.')

    def has_object_permission(self, request, view, obj):
        if not hasattr(obj, 'institution'):
            return True
        user = request.user
        if user.role == user.Role.ADMIN:
            return True
        return obj.institution_id == user.institution_id


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