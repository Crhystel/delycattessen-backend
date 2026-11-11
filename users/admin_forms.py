"""Forms of the superadmin user screens.

Rules mirrored from the API (users/serializers.py): e-mail in lower case and
unique ignoring case, names in upper case, and a sede for every staff role.
"""
from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from .models import CustomUser

Role = CustomUser.Role

# Roles that have a sede and no profile table; the superadmin can create them.
STAFF_ROLES = (Role.ADMIN, Role.OPERATIONS_STAFF, Role.TEACHER)
NAME_FIELDS = ('first_name', 'second_name', 'last_name', 'second_last_name')
NAME_LABELS = {
    'first_name': _('Primer nombre'),
    'second_name': _('Segundo nombre'),
    'last_name': _('Primer apellido'),
    'second_last_name': _('Segundo apellido'),
}


class AccountFieldsMixin:
    """Shared cleaning of the e-mail and the names."""

    def clean_email(self):
        email = (self.cleaned_data.get('email') or '').strip().lower()
        if not email:
            return None
        clash = CustomUser.objects.filter(email__iexact=email).exclude(pk=self.instance.pk)
        if clash.exists():
            raise ValidationError(_('Ya existe una cuenta con este correo.'))
        return email

    def clean(self):
        cleaned = super().clean()
        for field in NAME_FIELDS:
            if cleaned.get(field):
                cleaned[field] = cleaned[field].strip().upper()
        return cleaned


class StaffAccountCreationForm(AccountFieldsMixin, forms.ModelForm):
    """Creates an administrator, teacher or operations-staff account. The service
    behind the API generates the temporary password and mails it to the new user."""

    class Meta:
        model = CustomUser
        fields = ('email', 'first_name', 'second_name', 'last_name', 'second_last_name', 'role', 'institution')
        labels = {**NAME_LABELS, 'email': _('Correo'), 'role': _('Rol'), 'institution': _('Institución')}
        help_texts = {'email': _('La contraseña temporal se envía a este correo.')}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['role'].choices = [(value, label) for value, label in Role.choices if value in STAFF_ROLES]
        self.fields['role'].initial = Role.TEACHER
        for required in ('email', 'first_name', 'last_name', 'institution'):
            self.fields[required].required = True


class CustomUserChangeForm(AccountFieldsMixin, forms.ModelForm):
    class Meta:
        model = CustomUser
        fields = '__all__'
        labels = {
            **NAME_LABELS,
            'email': _('Correo'),
            'institution': _('Institución'),
            'must_change_password': _('Debe cambiar la contraseña'),
            'is_active': _('Activa'),
            'is_staff': _('Acceso al panel técnico (Django)'),
            'is_superuser': _('Superadministrador'),
        }

    def clean(self):
        cleaned = super().clean()
        role = self.instance.role
        if role in STAFF_ROLES and 'institution' in self.fields and not cleaned.get('institution'):
            self.add_error('institution', _('Esta cuenta necesita una institución.'))
        return cleaned
