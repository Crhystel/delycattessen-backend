from rest_framework.permissions import BasePermission
from django.utils.translation import gettext_lazy as _


class SameInstitutionPermission(BasePermission):
    message = _('No tiene acceso a la información de esta institución.')

    def has_object_permission(self, request, view, obj):
        if not hasattr(obj, 'institution'):
            return True
        return obj.institution_id == request.user.institution_id


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