import secrets

from django.conf import settings
from django.core.cache import cache
from rest_framework.permissions import SAFE_METHODS

CODE_TTL_SECONDS = settings.PASSWORD_RESET_TIMEOUT
MAX_CODE_ATTEMPTS = 5


def reset_code_key(email: str) -> str:
    return f'password_reset_code:{email}'


def reset_attempts_key(email: str) -> str:
    return f'password_reset_attempts:{email}'


ALL_INSTITUTIONS = 'all'


def resolve_institution_scope(request):
    """
    Single source of truth for institution (sede) data scope.

    Returns an Institution to filter by, or None meaning "all institutions".
    - ADMIN: own institution by default; may ask for a specific one
      (?institution=<id>) or the consolidated view (?institution=all).
    - Any other role: always its own institution; the query param is ignored.
    Raises PermissionDenied if the user has no institution and did not
    explicitly request one (admins only), ValidationError on a bad param.
    """
    from rest_framework.exceptions import PermissionDenied, ValidationError
    from django.utils.translation import gettext as _
    from .models import CustomUser, Institution

    user = request.user
    if user.role == CustomUser.Role.ADMIN:
        param = request.query_params.get('institution')
        if param == ALL_INSTITUTIONS:
            return None
        if param:
            try:
                return Institution.objects.get(pk=int(param))
            except (ValueError, Institution.DoesNotExist):
                raise ValidationError({'institution': _('Institución inválida.')})

    institution = getattr(user, 'institution', None)
    if institution is None:
        raise PermissionDenied(_('No tiene una institución asignada para realizar esta acción.'))
    return institution


def resolve_write_institution(request):
    """
    Institution where a write (create/update/delete) lands. Never "all":
    - ADMIN: the institution given in ?institution=<id>, otherwise (no param
      or ?institution=all) their own institution.
    - Any other role: always its own institution; the query param is ignored.
    Raises PermissionDenied if the resolved user has no institution and
    ValidationError on a bad param (both from resolve_institution_scope).
    """
    from rest_framework.exceptions import PermissionDenied
    from django.utils.translation import gettext as _

    institution = resolve_institution_scope(request)
    if institution is not None:
        return institution

    own = getattr(request.user, 'institution', None)
    if own is None:
        raise PermissionDenied(_('No tiene una institución asignada para realizar esta acción.'))
    return own


class ViewerInstitutionMixin:
    """
    Mixin Pattern for read-only endpoints open to every role (e.g. the product
    menu): the institution whose data is shown depends on who is asking.

    - STUDENT: the institution of their own student profile.
    - PARENT: the institution of the child given in ?student_id= (must be
      their own child).
    - ADMIN / staff / teacher: resolve_institution_scope (admin may widen it).
    Returns an Institution, or None for the consolidated admin view.
    """
    institution_lookup = 'institution'

    def get_viewer_institution(self):
        from rest_framework.exceptions import PermissionDenied, ValidationError
        from django.utils.translation import gettext as _
        from .models import CustomUser, StudentProfile

        user = self.request.user
        if user.role == CustomUser.Role.STUDENT:
            profile = getattr(user, 'student_profile', None)
            if profile is None:
                raise PermissionDenied(_('No tiene una institución asignada para realizar esta acción.'))
            return profile.institution

        if user.role == CustomUser.Role.PARENT:
            student_id = self.request.query_params.get('student_id')
            if not student_id:
                raise ValidationError({'student_id': _('Debe indicar el estudiante.')})
            try:
                student = StudentProfile.objects.select_related('institution').get(
                    pk=int(student_id), parent__user=user,
                )
            except (ValueError, StudentProfile.DoesNotExist):
                raise PermissionDenied(_('No tienes permiso sobre este estudiante'))
            return student.institution

        return resolve_institution_scope(self.request)

    def get_queryset(self):
        queryset = super().get_queryset()
        institution = self.get_viewer_institution()
        if institution is None:
            return queryset
        return queryset.filter(**{self.institution_lookup: institution})


class TokenGeneratorMixin:
    """
    Mixin Pattern: abstracts the repetitive logic of generating cryptographic
    tokens and simulating email delivery. Reusable by any view that
    requires this behavior.
    """
    def generate_and_send_token(self, user, email: str) -> None:
        code = f'{secrets.randbelow(1_000_000):06d}'
        cache.set(reset_code_key(email), code, timeout=CODE_TTL_SECONDS)
        cache.delete(reset_attempts_key(email))
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
    to the authenticated user's institution. Only an ADMIN may widen or move the read
    scope with ?institution=<id> or ?institution=all (resolve_institution_scope).
    Writes land in the target institution (resolve_write_institution): an
    ADMIN's ?institution=<id>, otherwise their own; never "all".
    get_user_institution() (used by perform_create and the write queryset)
    returns that target.
    """
    institution_lookup = 'institution'
    allowed_roles = None  # None = every role; otherwise only these get data

    def get_user_institution(self):
        return resolve_write_institution(self.request)

    def get_serializer_context(self):
        """Hands the write target to serializers so validations (duplicates,
        dish ownership) run against it instead of the user's own sede."""
        context = super().get_serializer_context()
        if self.request.method not in SAFE_METHODS:
            context['institution'] = self.get_user_institution()
        return context

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.allowed_roles is not None and self.request.user.role not in self.allowed_roles:
            return queryset.none()
        if self.request.method in SAFE_METHODS:
            institution = resolve_institution_scope(self.request)
        else:
            institution = self.get_user_institution()
        if institution is None:
            return queryset
        return queryset.filter(**{self.institution_lookup: institution})


class InstitutionScopeMixin(OwnInstitutionMixin):
    """
    OwnInstitutionMixin restricted to the roles that manage staff data
    (operations staff and admins); any other role gets an empty queryset.
    """
    allowed_roles = ('OPERATIONS_STAFF', 'ADMIN')
