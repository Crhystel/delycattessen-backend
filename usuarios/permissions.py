from rest_framework.permissions import BasePermission

class MismaSedePermission(BasePermission):
    message = "No tiene acceso a información de esta sede."

    def has_object_permission(self, request, view, obj):
        if not hasattr(obj, 'sede'):
            return True
        return obj.sede_id == request.user.sede_id