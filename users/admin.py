"""Superadmin of the people in the system.

Accounts are never hard-deleted here: a deletion would cascade into wallets,
transactions and pre-orders. They are deactivated or soft-deleted through the same
service the API uses (soft_delete_account). Parents and students are registered from
the app, which creates their profile and wallet together, so they are not created here.
"""
from django.contrib import admin, messages
from django.contrib.admin.utils import flatten_fieldsets
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Count
from django.utils.translation import gettext_lazy as _

from core.admin_mixins import NoDeleteAdminMixin, ReadOnlyAdminMixin, ReadOnlyInlineMixin

from .admin_forms import STAFF_ROLES, CustomUserChangeForm, StaffAccountCreationForm
from .models import CustomUser, Institution, ParentalControl, ParentProfile, StudentProfile, UserBiometric
from .services import (
    create_staff_account,
    generate_temporary_password,
    notify_staff_credentials,
    revoke_user_tokens,
    soft_delete_account,
)

Role = CustomUser.Role


def full_name(user):
    parts = [user.first_name, user.second_name, user.last_name, user.second_last_name]
    return ' '.join(part for part in parts if part) or user.username


class DeletedFilter(admin.SimpleListFilter):
    title = _('baja lógica')
    parameter_name = 'baja'

    def lookups(self, request, model_admin):
        return [('si', _('Dados de baja')), ('no', _('Vigentes'))]

    def queryset(self, request, queryset):
        if self.value() == 'si':
            return queryset.filter(deleted_at__isnull=False)
        if self.value() == 'no':
            return queryset.filter(deleted_at__isnull=True)
        return queryset


class MissingProfileFilter(admin.SimpleListFilter):
    """Finds accounts left half-created (e.g. a student whose profile was removed)."""

    title = _('perfil faltante')
    parameter_name = 'sin_perfil'

    def lookups(self, request, model_admin):
        return [('estudiante', _('Estudiantes sin perfil')), ('padre', _('Padres sin perfil'))]

    def queryset(self, request, queryset):
        if self.value() == 'estudiante':
            return queryset.filter(role=Role.STUDENT, student_profile__isnull=True)
        if self.value() == 'padre':
            return queryset.filter(role=Role.PARENT, parent_profile__isnull=True)
        return queryset


class ParentProfileInline(ReadOnlyInlineMixin, admin.StackedInline):
    model = ParentProfile
    verbose_name = _('perfil de padre')
    verbose_name_plural = _('perfil de padre')
    fields = ('children_summary', 'has_payment_pin')
    readonly_fields = fields

    @admin.display(description=_('Hijos'))
    def children_summary(self, obj):
        names = [full_name(child.user) for child in obj.children.select_related('user')]
        return ', '.join(names) or _('Sin hijos registrados')

    @admin.display(description=_('PIN de pago configurado'), boolean=True)
    def has_payment_pin(self, obj):
        return bool(obj.payment_pin_hash)


class StudentProfileInline(ReadOnlyInlineMixin, admin.StackedInline):
    model = StudentProfile
    verbose_name = _('perfil de estudiante')
    verbose_name_plural = _('perfil de estudiante')
    fields = ('institution', 'parent', 'wallet_balance', 'parental_control_summary')
    readonly_fields = fields

    @admin.display(description=_('Saldo de la billetera'))
    def wallet_balance(self, obj):
        try:
            return f'$ {obj.wallet.balance}'
        except ObjectDoesNotExist:
            return _('Sin billetera')

    @admin.display(description=_('Control parental'))
    def parental_control_summary(self, obj):
        try:
            control = obj.parental_control
        except ObjectDoesNotExist:
            return _('Sin configurar')
        limit = f'$ {control.daily_limit_amount}' if control.daily_limit_enabled else _('sin límite')
        return _('Límite diario: %(limit)s · aviso de saldo bajo: %(alert)s') % {
            'limit': limit,
            'alert': _('sí') if control.low_balance_alert_enabled else _('no'),
        }


@admin.register(CustomUser)
class CustomUserAdmin(NoDeleteAdminMixin, admin.ModelAdmin):
    form = CustomUserChangeForm
    add_form = StaffAccountCreationForm

    list_display = ('display_name', 'email', 'role', 'institution', 'is_active', 'must_change_password', 'is_soft_deleted')
    list_filter = ('role', 'institution', 'is_active', DeletedFilter, MissingProfileFilter)
    search_fields = ('username', 'email', 'first_name', 'second_name', 'last_name', 'second_last_name')
    ordering = ('role', 'last_name', 'first_name')
    list_select_related = ('institution',)
    list_per_page = 50
    actions = ('activate_accounts', 'deactivate_accounts', 'soft_delete_staff', 'reset_temporary_password')

    fieldsets = (
        (_('Cuenta'), {'fields': ('username', 'email')}),
        (_('Datos personales'), {'fields': ('first_name', 'second_name', 'last_name', 'second_last_name')}),
        (_('Rol y sede'), {'fields': ('role', 'institution')}),
        (_('Estado'), {'fields': ('is_active', 'must_change_password', 'deleted_at')}),
        (_('Acceso al panel técnico'), {'fields': ('is_staff', 'is_superuser')}),
        (_('Actividad'), {'fields': ('last_login', 'date_joined')}),
    )
    add_fieldsets = (
        (_('Nueva cuenta de personal'), {
            'description': _(
                'Administrador, docente o personal operativo. Los padres y los estudiantes se '
                'registran desde la app, que crea su perfil y su billetera.'
            ),
            'fields': ('email', 'first_name', 'second_name', 'last_name', 'second_last_name', 'role', 'institution'),
        }),
    )

    # ------------------------------------------------------------------ list
    @admin.display(description=_('Nombre'), ordering='last_name')
    def display_name(self, obj):
        return full_name(obj)

    @admin.display(description=_('Dado de baja'), boolean=True, ordering='deleted_at')
    def is_soft_deleted(self, obj):
        return obj.deleted_at is not None

    # ------------------------------------------------------------------ form
    def get_fieldsets(self, request, obj=None):
        return self.add_fieldsets if obj is None else self.fieldsets

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            kwargs['form'] = self.add_form
        return super().get_form(request, obj, **kwargs)

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return ()
        if obj.deleted_at:
            return flatten_fieldsets(self.fieldsets)  # a soft-deleted account is never edited
        readonly = ['username', 'role', 'deleted_at', 'last_login', 'date_joined']
        if obj.role not in STAFF_ROLES:
            readonly.append('institution')  # a parent has none; a student's sede lives in the profile
        if not request.user.is_superuser or obj.pk == request.user.pk:
            readonly += ['is_active', 'is_staff', 'is_superuser']
        return readonly

    def get_inline_instances(self, request, obj=None):
        if obj is None:
            return []
        inline = {Role.PARENT: ParentProfileInline, Role.STUDENT: StudentProfileInline}.get(obj.role)
        return [inline(self.model, self.admin_site)] if inline else []

    def save_model(self, request, obj, form, change):
        if not change:
            self._create_staff(request, obj, form)
            return
        if 'email' in form.changed_data and obj.role in (Role.TEACHER, Role.OPERATIONS_STAFF) and obj.email:
            obj.username = obj.email  # same rule as the staff API
        super().save_model(request, obj, form, change)

    def _create_staff(self, request, obj, form):
        """Goes through the API service so the account gets a temporary password,
        must_change_password and the credentials e-mail like any other staff account."""
        user, temporary_password = create_staff_account(form.cleaned_data, admin=request.user)
        obj.pk = user.pk
        obj.refresh_from_db()
        obj._state.adding = False
        if notify_staff_credentials(user, temporary_password):
            self.message_user(request, _('Cuenta creada. La contraseña temporal se envió por correo.'))
        else:
            self.message_user(
                request,
                _('Cuenta creada, pero no se pudo enviar el correo. Usa "Restablecer contraseña temporal" más tarde.'),
                messages.WARNING,
            )

    # --------------------------------------------------------------- actions
    @admin.action(description=_('Activar las cuentas seleccionadas'), permissions=['change'])
    def activate_accounts(self, request, queryset):
        updated = queryset.filter(deleted_at__isnull=True, is_active=False).update(is_active=True)
        self.message_user(request, _('%(n)d cuenta(s) activada(s).') % {'n': updated})

    @admin.action(description=_('Desactivar las cuentas seleccionadas (cierra sus sesiones)'), permissions=['change'])
    def deactivate_accounts(self, request, queryset):
        targets = queryset.filter(is_active=True, deleted_at__isnull=True, is_superuser=False)
        targets = targets.exclude(pk=request.user.pk)
        done = 0
        for user in targets:
            user.is_active = False
            user.save(update_fields=['is_active'])
            revoke_user_tokens(user)
            done += 1
        self.message_user(request, _('%(n)d cuenta(s) desactivada(s).') % {'n': done})

    @admin.action(description=_('Dar de baja (eliminación lógica) al personal seleccionado'), permissions=['change'])
    def soft_delete_staff(self, request, queryset):
        """Same policy as CanManageStaffAccount: operations staff and teachers still
        pending activation. An active teacher manages their own account."""
        done = skipped = 0
        for user in queryset:
            deletable = (
                user.deleted_at is None
                and not user.is_superuser
                and user.pk != request.user.pk
                and (user.role == Role.OPERATIONS_STAFF or (user.role == Role.TEACHER and user.must_change_password))
            )
            if not deletable:
                skipped += 1
                continue
            soft_delete_account(user)
            revoke_user_tokens(user)
            done += 1
        self.message_user(request, _('%(n)d cuenta(s) dada(s) de baja.') % {'n': done})
        if skipped:
            self.message_user(
                request,
                _('%(n)d no se dieron de baja: solo el personal operativo y los docentes que aún no activan '
                  'su cuenta (los demás se desactivan).') % {'n': skipped},
                messages.WARNING,
            )

    @admin.action(description=_('Restablecer contraseña temporal (se envía por correo)'), permissions=['change'])
    def reset_temporary_password(self, request, queryset):
        targets = queryset.filter(
            role__in=(Role.OPERATIONS_STAFF, Role.TEACHER), deleted_at__isnull=True, is_active=True,
            email__isnull=False,
        )
        done = 0
        for user in targets:
            temporary_password = generate_temporary_password()
            user.set_password(temporary_password)
            user.must_change_password = True
            user.save(update_fields=['password', 'must_change_password'])
            revoke_user_tokens(user)
            notify_staff_credentials(user, temporary_password)
            done += 1
        self.message_user(request, _('%(n)d contraseña(s) restablecida(s) y enviada(s) por correo.') % {'n': done})


@admin.register(Institution)
class InstitutionAdmin(NoDeleteAdminMixin, admin.ModelAdmin):
    list_display = ('name', 'users_total', 'students_total', 'registration_date')
    search_fields = ('name',)
    readonly_fields = ('registration_date',)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            users_count=Count('users', distinct=True), students_count=Count('students', distinct=True),
        )

    @admin.display(description=_('Cuentas de personal'), ordering='users_count')
    def users_total(self, obj):
        return obj.users_count

    @admin.display(description=_('Estudiantes'), ordering='students_count')
    def students_total(self, obj):
        return obj.students_count


@admin.register(ParentProfile)
class ParentProfileAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('parent_name', 'parent_email', 'children_total', 'has_payment_pin')
    search_fields = ('user__username', 'user__email', 'user__first_name', 'user__last_name')
    list_select_related = ('user',)
    exclude = ('payment_pin_hash',)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(children_count=Count('children'))

    @admin.display(description=_('Padre'), ordering='user__last_name')
    def parent_name(self, obj):
        return full_name(obj.user)

    @admin.display(description=_('Correo'), ordering='user__email')
    def parent_email(self, obj):
        return obj.user.email

    @admin.display(description=_('Hijos'), ordering='children_count')
    def children_total(self, obj):
        return obj.children_count

    @admin.display(description=_('PIN de pago'), boolean=True)
    def has_payment_pin(self, obj):
        return bool(obj.payment_pin_hash)


@admin.register(StudentProfile)
class StudentProfileAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('student_name', 'username', 'institution', 'parent_name', 'wallet_balance')
    list_filter = ('institution',)
    search_fields = ('user__username', 'user__first_name', 'user__last_name', 'parent__user__username')
    list_select_related = ('user', 'institution', 'parent__user', 'wallet')
    exclude = ('profile_picture', 'allergies')  # health and photo data belong to the family

    @admin.display(description=_('Estudiante'), ordering='user__last_name')
    def student_name(self, obj):
        return full_name(obj.user)

    @admin.display(description=_('Usuario'), ordering='user__username')
    def username(self, obj):
        return obj.user.username

    @admin.display(description=_('Padre'), ordering='parent__user__last_name')
    def parent_name(self, obj):
        return full_name(obj.parent.user)

    @admin.display(description=_('Saldo'))
    def wallet_balance(self, obj):
        try:
            return f'$ {obj.wallet.balance}'
        except ObjectDoesNotExist:
            return _('Sin billetera')


@admin.register(ParentalControl)
class ParentalControlAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('student', 'daily_limit_enabled', 'daily_limit_amount', 'allowed_days_enabled', 'low_balance_alert_enabled')
    list_select_related = ('student__user',)
    search_fields = ('student__user__username',)


@admin.register(UserBiometric)
class UserBiometricAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Only that a record exists: the encrypted vector is never shown."""

    list_display = ('user', 'is_active', 'created_at', 'updated_at')
    list_select_related = ('user',)
    search_fields = ('user__username',)
    exclude = ('encrypted_embedding', 'nonce', 'tag')
