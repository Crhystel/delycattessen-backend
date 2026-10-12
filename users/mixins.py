import random
from django.core.cache import cache
from django.contrib.auth.tokens import default_token_generator

CODE_TTL_SECONDS = 900  # 15 minutes


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
        code = f"{random.randint(0,999999):06d}"
        cache.set(f"password_reset_code:{email}", code, timeout=CODE_TTL_SECONDS)
        from .tasks import send_password_reset_email
        send_password_reset_email.delay(email, code)

class UppercaseNamesMixin:
    """Uppercases the given name fields in validated_data before
    create/update, so all registered names (students, staff, or any
    future user type) are stored consistently regardless of how the
    user typed them. Subclasses set `uppercase_fields`."""

    uppercase_fields = ()

    def validate(self, attrs):
        attrs = super().validate(attrs)
        for field in self.uppercase_fields:
            if field in attrs and attrs[field]:
                attrs[field] = attrs[field].upper()
        return attrs


class OwnInstitutionMixin:
    """
    Mixin Pattern (over DRF's get_queryset): enforces multi-tenant scoping
    strictly to the authenticated user's institution (request.user.institution).
    Ignores any ?institution= query param.
    Raises PermissionDenied if the user does not have an institution assigned.
    Provides get_user_institution() helper for perform_create.
    """
    institution_lookup = 'institution'

    def get_user_institution(self):
        from rest_framework.exceptions import PermissionDenied
        user = self.request.user
        institution = getattr(user, 'institution', None)
        if not institution:
            raise PermissionDenied('No tiene una institución asignada para realizar esta acción.')
        return institution

    def get_queryset(self):
        queryset = super().get_queryset()
        institution = self.get_user_institution()
        return queryset.filter(**{self.institution_lookup: institution})
