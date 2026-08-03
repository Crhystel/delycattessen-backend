class AislamientoPorSedeMixin:
    """
    Aplica exclusivamente a Administrador y Personal Operativo.
    """
    def get_queryset(self):
        queryset = super().get_queryset()
        usuario = self.request.user
        from .models import CustomUser
        if usuario.rol in [CustomUser.Role.ADMIN, CustomUser.Role.PERSONAL_OPERATIVO]:
            return queryset.filter(sede=usuario.sede)
        return queryset.none()