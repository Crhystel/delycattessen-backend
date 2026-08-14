from rest_framework.permissions import BasePermission


class IsWalletOwnerParent(BasePermission):
    """Solo el padre de familia dueño del hijo puede operar sobre esa
    billetera. Reutilizable en cualquier endpoint de wallet/ que reciba un
    objeto Wallet (recarga, historial, límites)."""

    message = 'No tienes permiso para operar sobre esta billetera.'

    def has_object_permission(self, request, view, obj):
      return obj.student.parent_id == request.user.id