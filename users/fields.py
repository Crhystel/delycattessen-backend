from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers


class PasswordField(serializers.CharField):
    """Write-only password field that always enforces the project password policy."""

    def __init__(self, **kwargs):
        kwargs.setdefault('write_only', True)
        kwargs.setdefault('style', {'input_type': 'password'})
        kwargs['trim_whitespace'] = False
        kwargs['validators'] = [validate_password, *kwargs.get('validators', [])]
        super().__init__(**kwargs)


class AcceptTermsField(serializers.BooleanField):
    """Write-only checkbox of the terms and conditions: only `true` is valid."""

    default_error_messages = {'not_accepted': 'Debes aceptar los términos y condiciones.'}

    def __init__(self, **kwargs):
        kwargs.setdefault('write_only', True)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if value is not True:
            self.fail('not_accepted')
        return value
