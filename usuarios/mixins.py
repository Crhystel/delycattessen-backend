from django.contrib.auth.tokens import default_token_generator


class InstitutionScopeMixin:
    """
    Mixin Pattern (Template Method over DRF's get_queryset): defines the
    data scope based on role and an optional 'institution' query parameter.

    - Operations Staff: always restricted to their own institution.
    - Administrator: defaults to their own institution; can request a
      specific institution (?institution=<id>) or a consolidated view
      across all institutions (?institution=all).
    """
    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        from .models import CustomUser

        if user.role == CustomUser.Role.OPERATIONS_STAFF:
            return queryset.filter(institution=user.institution)

        if user.role == CustomUser.Role.ADMIN:
            institution_param = self.request.query_params.get('institution')
            if institution_param == 'all':
                return queryset
            if institution_param:
                return queryset.filter(institution_id=institution_param)
            return queryset.filter(institution=user.institution)

        return queryset.none()


class TokenGeneratorMixin:
    """
    Mixin Pattern: abstracts the repetitive logic of generating cryptographic
    tokens and simulating email delivery. Reusable by any view that
    requires this behavior.
    """
    def generate_and_send_token(self, user, email: str) -> None:
        token = default_token_generator.make_token(user)

        # Simulación de envío de correo en la consola
        print(f"\n{'='*50}\nSIMULACIÓN DE CORREO A: {email}")
        print(f"Tu código/token de seguridad es: {token}")
        print(f"Este código es de uso único y tiene vigencia limitada.\n{'='*50}\n")