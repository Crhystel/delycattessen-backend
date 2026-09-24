import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

MIN_LENGTH = 8


class PasswordPolicyValidator:
    """Single source of truth for the password policy (mirrors the Flutter/React rules)."""

    rules = (
        ('password_no_upper', r'[A-Z]', _('Al menos una letra mayúscula')),
        ('password_no_lower', r'[a-z]', _('Al menos una letra minúscula')),
        ('password_no_digit', r'[0-9]', _('Al menos un número')),
        ('password_no_special', r'[^A-Za-z0-9]', _('Al menos un carácter especial (ej. ! @ # $ %)')),
    )

    def validate(self, password, user=None):
        errors = []
        if len(password) < MIN_LENGTH:
            errors.append(ValidationError(
                _('Mínimo %(min)d caracteres'),
                code='password_too_short',
                params={'min': MIN_LENGTH},
            ))
        errors += [
            ValidationError(message, code=code)
            for code, pattern, message in self.rules
            if not re.search(pattern, password)
        ]
        if errors:
            raise ValidationError(errors)

    def get_help_text(self):
        return _('Mínimo %(min)d caracteres, con mayúscula, minúscula, número y carácter especial.') % {'min': MIN_LENGTH}