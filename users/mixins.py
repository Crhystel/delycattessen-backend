import secrets

from django.conf import settings
from django.core.cache import cache

CODE_TTL_SECONDS = settings.PASSWORD_RESET_TIMEOUT
MAX_CODE_ATTEMPTS = 5


def reset_code_key(email: str) -> str:
    return f'password_reset_code:{email}'


def reset_attempts_key(email: str) -> str:
    return f'password_reset_attempts:{email}'


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
