"""Forms of the superadmin user and sede screens.

Rules mirrored from the API (users/serializers.py): e-mail in lower case and
unique ignoring case, names in upper case, and a sede for every staff role.
"""
from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from .admin_services import STAFF_ROLES, role_change_error
from .models import CustomUser, Institution

Role = CustomUser.Role

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
        # A suspended sede takes no new accounts.
        self.fields['institution'].queryset = Institution.objects.filter(is_active=True)


class CustomUserChangeForm(AccountFieldsMixin, forms.ModelForm):
    # Set by CustomUserAdmin.get_form: who is editing (a superadmin cannot change their own role).
    actor = None

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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'role' in self.fields and self.instance.role in STAFF_ROLES:
            self.fields['role'].choices = [(value, label) for value, label in Role.choices if value in STAFF_ROLES]
            self.fields['role'].help_text = _(
                'Solo entre docente, personal operativo y administrador. Se cierran sus sesiones al cambiarlo.'
            )

    def clean(self):
        cleaned = super().clean()
        role = cleaned.get('role', self.instance.role)
        if 'role' in self.fields and role != self.instance.role:
            message = role_change_error(self.instance, role, self.actor)
            if message:
                self.add_error('role', message)
        if role in STAFF_ROLES and 'institution' in self.fields and not cleaned.get('institution'):
            self.add_error('institution', _('Esta cuenta necesita una institución.'))
        return cleaned


class InstitutionForm(forms.ModelForm):
    class Meta:
        model = Institution
        fields = '__all__'

    def clean_name(self):
        name = (self.cleaned_data['name'] or '').strip()
        clash = Institution.objects.filter(name__iexact=name).exclude(pk=self.instance.pk)
        if clash.exists():
            raise ValidationError(_('Ya existe una institución con este nombre.'))
        return name

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk and self.instance.is_active and not cleaned.get('is_active', True):
            if not (cleaned.get('suspension_reason') or '').strip():
                self.add_error('suspension_reason', _('Indica el motivo para suspender la sede.'))
        return cleaned
