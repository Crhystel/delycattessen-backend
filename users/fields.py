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