from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils.translation import gettext_lazy as _
from django.contrib.auth.hashers import check_password, make_password


class Institution(models.Model):
    name = models.CharField(_('name'), max_length=255, unique=True)
    registration_date = models.DateTimeField(_('registration date'), auto_now_add=True)

    def __str__(self) -> str:
        return self.name

    class Meta:
        verbose_name = _('institution')
        verbose_name_plural = _('institutions')


class CustomUser(AbstractUser):
    class Role(models.TextChoices):
        PARENT = 'PARENT', _('Padre de Familia')
        STUDENT = 'STUDENT', _('Estudiante')
        ADMIN = 'ADMIN', _('Administrador')
        OPERATIONS_STAFF = 'OPERATIONS_STAFF', _('Personal Operativo')
        TEACHER = 'TEACHER', _('Docente')

    email = models.EmailField(_('email address'), unique=True, null=True, blank=True)
    second_name = models.CharField(_('second name'), max_length=150, blank=True)
    second_last_name = models.CharField(_('second last name'), max_length=150, blank=True)
    role = models.CharField(_('role'), max_length=20, choices=Role.choices, default=Role.STUDENT)
    institution = models.ForeignKey(
        Institution,
        verbose_name=_('institution'),
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
    EMAIL_FIELD = 'email'

    def __str__(self) -> str:
        return self.username


class ParentProfile(models.Model):
    user = models.OneToOneField(
        CustomUser, verbose_name=_('user'), on_delete=models.CASCADE, related_name='parent_profile'
    )
    payment_pin_hash = models.CharField(_('payment PIN hash'), max_length=128, blank=True)

    def set_payment_pin(self, raw_pin: str) -> None:
        self.payment_pin_hash = make_password(raw_pin)
        self.save(update_fields=['payment_pin_hash'])

    def check_payment_pin(self, raw_pin: str) -> bool:
        if not self.payment_pin_hash:
            return False
        return check_password(raw_pin, self.payment_pin_hash)

    def __str__(self) -> str:
        return str(_('Perfil de Padre: %(name)s')) % {
            'name': self.user.get_full_name() or self.user.username
        }

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
    allergies = models.ManyToManyField(
        'catalog.Allergen', verbose_name=_('allergies'), blank=True, related_name='students'
    )

    def __str__(self) -> str:
        return str(_('Perfil de Estudiante: %(username)s')) % {'username': self.user.username}

    class Meta:
        verbose_name = _('student profile')
        verbose_name_plural = _('student profiles')
        
class Allergen(models.Model):
    """Catalog of common allergens — seeded via data migration."""

    name = models.CharField(_('name'), max_length=100, unique=True)

    def __str__(self) -> str:
        return self.name

    class Meta:
        verbose_name = _('allergen')
        verbose_name_plural = _('allergens')


class UserAllergy(models.Model):
    """Links a CustomUser (student or teacher) to an allergen they have.
    Registered by a parent (for their child) or by the teacher themself."""

    user = models.ForeignKey(
        CustomUser, verbose_name=_('user'), on_delete=models.CASCADE, related_name='allergies'
    )
    allergen = models.ForeignKey(
        Allergen, verbose_name=_('allergen'), on_delete=models.CASCADE, related_name='affected_users'
    )
    created_at = models.DateTimeField(_('created at'), auto_now_add=True)

    class Meta:
        unique_together = ('user', 'allergen')
        verbose_name = _('user allergy')
        verbose_name_plural = _('user allergies')

    def __str__(self) -> str:
        return f'{self.user.username} - {self.allergen.name}'