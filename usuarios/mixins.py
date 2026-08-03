from django.contrib.auth.tokens import default_token_generator


class InstitutionIsolationMixin:
    """
    Mixin Pattern (Template Method over DRF's get_queryset): restricts
    access to data based on the authenticated user's institution.
    Applies exclusively to Administrator and Operations Staff.
    """
    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        from .models import CustomUser
        if user.role in [CustomUser.Role.ADMIN, CustomUser.Role.OPERATIONS_STAFF]:
            return queryset.filter(institution=user.institution)
        return queryset.none()


class TokenGeneratorMixin:
    """
    Mixin Pattern: abstracts the repetitive logic of generating cryptographic
    tokens and simulating email delivery. Reusable by any view that
    requires this behavior.
    """
    def generate_and_send_token(self, user, email: str) -> None:
        """
        Generates a secure token and simulates its delivery.
        """
        token = default_token_generator.make_token(user)

        # Simulación de envío de correo en la consola
        print(f"\n{'='*50}\nSIMULACIÓN DE CORREO A: {email}")
        print(f"Tu código/token de seguridad es: {token}")
        print(f"Este código es de uso único y tiene vigencia limitada.\n{'='*50}\n")