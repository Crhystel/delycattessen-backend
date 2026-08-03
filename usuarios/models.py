from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils.translation import gettext_lazy as _


class Location(models.Model):
    name = models.CharField(_('name'), max_length=100, unique=True)

    class Meta:
        verbose_name = _('location')
        verbose_name_plural = _('locations')

    def __str__(self) -> str:
        return self.name


class CustomUser(AbstractUser):
    class Role(models.TextChoices):
        PARENT = 'PARENT', _('Padre de Familia')
        STUDENT = 'STUDENT', _('Estudiante')
        ADMIN = 'ADMIN', _('Administrador')
        OPERATIONS_STAFF = 'OPERATIONS_STAFF', _('Personal Operativo')
        TEACHER = 'TEACHER', _('Docente')

    email = models.EmailField(_('email address'), unique=True)
    role = models.CharField(_('role'), max_length=20, choices=Role.choices, default=Role.STUDENT)
    location = models.ForeignKey(
        Location,
        verbose_name=_('location'),
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='users',
        help_text=_('Aplica únicamente a Administrador y Personal Operativo')
    )
    must_change_password = models.BooleanField(
        _('must change password'),
        default=False,
        help_text=_('Verdadero para cuentas creadas por el Administrador (Docente/Personal Operativo)')
    )

    def __str__(self) -> str:
        return self.username


class Institution(models.Model):
    name = models.CharField(_('name'), max_length=255, unique=True)
    registration_date = models.DateTimeField(_('registration date'), auto_now_add=True)

    def __str__(self) -> str:
        return self.name

    class Meta:
        verbose_name = _('institution')
        verbose_name_plural = _('institutions')


class ParentProfile(models.Model):
    user = models.OneToOneField(
        CustomUser, verbose_name=_('user'), on_delete=models.CASCADE, related_name='parent_profile'
    )

    def __str__(self) -> str:
        return f"Perfil de Padre: {self.user.get_full_name() or self.user.username}"

    class Meta:
        verbose_name = _('parent profile')
        verbose_name_plural = _('parent profiles')


class StudentProfile(models.Model):
    user = models.OneToOneField(
        CustomUser, verbose_name=_('user'), on_delete=models.CASCADE, related_name='student_profile'
    )
    institution = models.ForeignKey(
        Institution, verbose_name=_('institution'), on_delete=models.PROTECT, related_name='students'
    )
    profile_picture = models.ImageField(
        _('profile picture'), upload_to='profiles/students/', blank=False, null=False
    )
    parent = models.ForeignKey(
        ParentProfile, verbose_name=_('parent'), on_delete=models.CASCADE, related_name='children'
    )

    def __str__(self) -> str:
        return f"Perfil de Estudiante: {self.user.username}"

    class Meta:
        verbose_name = _('student profile')
        verbose_name_plural = _('student profiles')