from django.db import models
from django.db.models.functions import Lower
from django.contrib.auth.models import AbstractUser, UserManager
from django.utils.translation import gettext_lazy as _
from django.contrib.auth.hashers import check_password, make_password


class Institution(models.Model):
    name = models.CharField(_('name'), max_length=255, unique=True)
    code = models.CharField(_('código'), max_length=20, unique=True, null=True, blank=True)
    address = models.CharField(_('dirección'), max_length=255, blank=True)
    phone = models.CharField(_('teléfono'), max_length=30, blank=True)
    contact_name = models.CharField(_('persona de contacto'), max_length=150, blank=True)
    contact_email = models.EmailField(_('correo de contacto'), blank=True)
    # A suspended sede is hidden from new registrations (students, staff); its existing
    # accounts, sessions and sales keep working (see InstitutionAdmin).
    is_active = models.BooleanField(_('activa'), default=True)
    suspended_at = models.DateTimeField(_('suspendida el'), null=True, blank=True)
    suspension_reason = models.CharField(_('motivo de la suspensión'), max_length=255, blank=True)
    registration_date = models.DateTimeField(_('registration date'), auto_now_add=True)

    def __str__(self) -> str:
        return self.name

    class Meta:
        verbose_name = _('institución')
        verbose_name_plural = _('instituciones')
        constraints = [
            # `name` is already unique; this also treats "Colegio" and "colegio" as the same sede.
            models.UniqueConstraint(Lower('name'), name='users_institution_name_ci_unique'),
        ]


class CustomUserManager(UserManager):
    def create_superuser(self, username, email=None, password=None, **extra_fields):
        # `createsuperuser` would otherwise leave the default role (STUDENT).
        extra_fields.setdefault('role', 'ADMIN')
        return super().create_superuser(username, email, password, **extra_fields)


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
    allergies = models.ManyToManyField(
        'catalog.Allergen', verbose_name=_('allergies'), blank=True, related_name='teachers',
        help_text=_('Aplica únicamente a Docentes')
    )
    deleted_at = models.DateTimeField(
        _('deleted at'), null=True, blank=True,
        help_text=_('Eliminación lógica: la cuenta se conserva para el historial pero ya no existe para el sistema.')
    )
    EMAIL_FIELD = 'email'

    objects = CustomUserManager()

    def __str__(self) -> str:
        return self.username

    class Meta(AbstractUser.Meta):
        verbose_name = _('usuario')
        verbose_name_plural = _('usuarios')
        constraints = [
            # The column is case-sensitive; this makes "A@x.com" and "a@x.com" the same
            # address, which EmailBackend (email__iexact) needs to find a single user.
            models.UniqueConstraint(
                Lower('email'),
                condition=models.Q(email__isnull=False),
                name='users_customuser_email_ci_unique',
            ),
            # Keep in sync with CustomUser.Role (Meta cannot see the enclosing class).
            models.CheckConstraint(
                condition=models.Q(
                    role__in=['PARENT', 'STUDENT', 'ADMIN', 'OPERATIONS_STAFF', 'TEACHER']
                ),
                name='users_customuser_role_valid',
            ),
            # A soft-deleted account can never be active again.
            models.CheckConstraint(
                condition=models.Q(deleted_at__isnull=True) | models.Q(is_active=False),
                name='users_customuser_deleted_is_inactive',
            ),
        ]


class ParentProfile(models.Model):
    # PROTECT: deleting the account must never cascade into the children's wallets.
    user = models.OneToOneField(
        CustomUser, verbose_name=_('user'), on_delete=models.PROTECT, related_name='parent_profile'
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
        verbose_name = _('perfil de padre')
        verbose_name_plural = _('perfiles de padres')

class StudentProfile(models.Model):
    # PROTECT: the wallet, its transactions and the pre-orders hang from this profile.
    user = models.OneToOneField(
        CustomUser, verbose_name=_('user'), on_delete=models.PROTECT, related_name='student_profile'
    )
    institution = models.ForeignKey(
        Institution, verbose_name=_('institution'), on_delete=models.PROTECT, related_name='students'
    )
    profile_picture = models.ImageField(
        _('profile picture'), upload_to='profiles/students/', blank=False, null=False
    )
    parent = models.ForeignKey(
        ParentProfile, verbose_name=_('parent'), on_delete=models.PROTECT, related_name='children'
    )
    allergies = models.ManyToManyField(
        'catalog.Allergen', verbose_name=_('allergies'), blank=True, related_name='students'
    )

    def __str__(self) -> str:
        return str(_('Perfil de Estudiante: %(username)s')) % {'username': self.user.username}

    def save(self, *args, **kwargs):
        if self.profile_picture:
            from delycattessen_backend.image_utils import resize_image_field
            resize_image_field(self.profile_picture)
        super().save(*args, **kwargs)

    class Meta:
        verbose_name = _('perfil de estudiante')
        verbose_name_plural = _('perfiles de estudiantes')

class ParentalControl(models.Model):
    student = models.OneToOneField(
        StudentProfile, verbose_name=_('student'), on_delete=models.CASCADE, related_name='parental_control'
    )
    daily_limit_enabled = models.BooleanField(_('daily limit enabled'), default=False)
    daily_limit_amount = models.DecimalField(_('daily limit amount'), max_digits=8, decimal_places=2, default=0.00)
    allowed_days_enabled = models.BooleanField(_('allowed days enabled'), default=False)
    allowed_days = models.JSONField(_('allowed days'), default=list, help_text=_('List of integers (0=Monday, 6=Sunday)'))
    low_balance_alert_enabled = models.BooleanField(_('low balance alert enabled'), default=False)
    low_balance_alert_amount = models.DecimalField(
        _('low balance alert amount'), max_digits=8, decimal_places=2, default=2.00,
        help_text=_('The parent is warned when the wallet balance drops below this amount'),
    )

    def __str__(self) -> str:
        return f"Parental Control for {self.student.user.username}"

    class Meta:
        verbose_name = _('control parental')
        verbose_name_plural = _('controles parentales')


class UserBiometric(models.Model):
    """Stores encrypted facial embeddings for student or teacher identification.
    Strictly complies with Data Protection Laws: NO raw photos are stored."""
    user = models.OneToOneField(
        CustomUser, verbose_name=_('user'), on_delete=models.CASCADE, related_name='biometric'
    )
    encrypted_embedding = models.BinaryField(_('encrypted embedding'))
    nonce = models.BinaryField(_('encryption nonce'), max_length=12)
    tag = models.BinaryField(_('encryption auth tag'), max_length=16)
    is_active = models.BooleanField(_('is active'), default=True)
    created_at = models.DateTimeField(_('created at'), auto_now_add=True)
    updated_at = models.DateTimeField(_('updated at'), auto_now=True)

    def __str__(self) -> str:
        return f"Biometric profile for {self.user.username}"

    class Meta:
        verbose_name = _('registro biométrico')
        verbose_name_plural = _('registros biométricos')


class AuditLog(models.Model):
    """Who did what and when for the actions that change accounts, passwords, roles and
    sedes (from the API, the superadmin or the system). Copies of the names are kept
    because accounts are anonymised when they are removed. `detail` never holds secrets."""

    class Action(models.TextChoices):
        ACCOUNT_CREATED = 'ACCOUNT_CREATED', _('Cuenta creada')
        ACCOUNT_ACTIVATED = 'ACCOUNT_ACTIVATED', _('Cuenta activada')
        ACCOUNT_DEACTIVATED = 'ACCOUNT_DEACTIVATED', _('Cuenta desactivada')
        ACCOUNT_REMOVED = 'ACCOUNT_REMOVED', _('Cuenta dada de baja')
        PASSWORD_RESET = 'PASSWORD_RESET', _('Contraseña restablecida')
        PASSWORD_CHANGED = 'PASSWORD_CHANGED', _('Contraseña cambiada')
        ROLE_CHANGED = 'ROLE_CHANGED', _('Rol cambiado')
        INSTITUTION_UPDATED = 'INSTITUTION_UPDATED', _('Sede editada')
        INSTITUTION_SUSPENDED = 'INSTITUTION_SUSPENDED', _('Sede suspendida')
        INSTITUTION_REACTIVATED = 'INSTITUTION_REACTIVATED', _('Sede reactivada')
        TASK_FAILED = 'TASK_FAILED', _('Tarea fallida')

    class Source(models.TextChoices):
        API = 'API', _('App o panel web')
        ADMIN = 'ADMIN', _('Panel superadmin')
        SYSTEM = 'SYSTEM', _('Sistema')

    actor = models.ForeignKey(
        CustomUser, verbose_name=_('actor'), on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    actor_label = models.CharField(_('actor'), max_length=150, blank=True)
    action = models.CharField(_('acción'), max_length=30, choices=Action.choices)
    target_type = models.CharField(_('tipo de objeto'), max_length=30, blank=True)
    target_id = models.PositiveBigIntegerField(_('id del objeto'), null=True, blank=True)
    target_label = models.CharField(_('objeto'), max_length=200, blank=True)
    institution = models.ForeignKey(
        Institution, verbose_name=_('institution'), on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    source = models.CharField(_('origen'), max_length=10, choices=Source.choices, default=Source.ADMIN)
    ip = models.GenericIPAddressField(_('IP'), null=True, blank=True)
    detail = models.JSONField(_('detalle'), default=dict, blank=True)
    created_at = models.DateTimeField(_('fecha'), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _('bitácora de acciones')
        verbose_name_plural = _('bitácora de acciones')
        ordering = ['-created_at']
        indexes = [models.Index(fields=['action', 'created_at'], name='users_audit_action_idx')]

    def __str__(self) -> str:
        return f'{self.get_action_display()} · {self.target_label or self.target_type} · {self.created_at:%Y-%m-%d %H:%M}'
