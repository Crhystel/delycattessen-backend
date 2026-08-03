from rest_framework.permissions import BasePermission

class CanRequestPasswordReset(BasePermission):
    """
    Custom Permission Class: Delega la autorización a una clase especializada.
    Aunque actualmente siempre retorna True (AllowAny), establece un contrato arquitectónico 
    para el futuro (ej. Rate Limiting, bloqueo de IPs, bloqueo de usuarios suspendidos).
    """
    
    def has_permission(self, request, view):
        # Aquí se podría validar si el usuario ha superado el límite de peticiones diarias
        # o si la IP está bloqueada. Por ahora permitimos el acceso público.
        return True
