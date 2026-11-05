from django.utils.translation import gettext_lazy as _
from rest_framework.permissions import BasePermission


class IsReportExportOwner(BasePermission):
    """Object-level: a generated report can only be read or downloaded by the administrator who asked for it."""
    message = _('Este reporte pertenece a otro usuario.')

    def has_object_permission(self, request, view, obj):
        return obj.requested_by_id == request.user.id
