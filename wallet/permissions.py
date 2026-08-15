from rest_framework.permissions import BasePermission


class IsWalletOwnerParent(BasePermission): 
    """Only a father or mother can access the wallet of their child."""
    message = 'No tienes permiso para operar sobre esta billetera.'
    def has_object_permission(self, request, view, obj):
        return obj.student.parent.user_id == request.user.id