"""Superadmin of the people in the system.

Accounts are never hard-deleted here: a deletion would cascade into wallets,
transactions and pre-orders. They are deactivated or soft-deleted through the same
service the API uses (soft_delete_account). Parents and students are registered from
the app, which creates their profile and wallet together, so they are not created here.
Everything the superadmin does to an account is also written to the audit log.
"""
from collections import Counter

from django.contrib import admin, messages
from django.contrib.admin.utils import flatten_fieldsets
from django.core.exceptions import ObjectDoesNotExist
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from core.admin_mixins import NoDeleteAdminMixin, ReadOnlyAdminMixin, ReadOnlyInlineMixin

from .admin_forms import CustomUserChangeForm, InstitutionForm, StaffAccountCreationForm
from .admin_services import (
    STAFF_ROLES,
    apply_role_change,
    reactivate_institution,
    reset_account_password,
    suspend_institution,
)
from .admin_stats import institution_stats
from .audit import display_name, record_audit
from .models import AuditLog, CustomUser, Institution, ParentProfile, StudentProfile
from .services import (
    create_staff_account,
    notify_staff_credentials,
    revoke_user_tokens,
    soft_delete_account,
)

Role = CustomUser.Role
RESET_LIMIT = 25  # accounts per "reset password" action, so one click cannot mail the whole school


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
        names = [display_name(child.user) for child in obj.children.select_related('user')]
        return ', '.join(names) or _('Sin hijos registrados')

    @admin.display(description=_('PIN de pago configurado'), boolean=True)
    def has_payment_pin(self, obj):
        return bool(obj.payment_pin_hash)


class StudentProfileInline(ReadOnlyInlineMixin, admin.StackedInline):
    model = StudentProfile
    verbose_name = _('perfil de estudiante')
    verbose_name_plural = _('perfil de estudiante')
    fields = ('institution', 'parent', 'wallet_balance')
    readonly_fields = fields

    @admin.display(description=_('Saldo de la billetera'))
    def wallet_balance(self, obj):
        try:
            return f'$ {obj.wallet.balance}'
        except ObjectDoesNotExist:
            return _('Sin billetera')


@admin.register(CustomUser)
class CustomUserAdmin(NoDeleteAdminMixin, admin.ModelAdmin):
    form = CustomUserChangeForm
    add_form = StaffAccountCreationForm

    list_display = (
        'display_name', 'email', 'role', 'institution', 'is_active', 'must_change_password',
        'last_login', 'is_soft_deleted',
    )
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
    own_password_fieldset = (_('Mi contraseña'), {'fields': ('own_password_link',)})
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
        return display_name(obj)

    @admin.display(description=_('Dado de baja'), boolean=True, ordering='deleted_at')
    def is_soft_deleted(self, obj):
        return obj.deleted_at is not None

    @admin.display(description=_('Contraseña'))
    def own_password_link(self, obj):
        return format_html(
            '<a href="{}">{}</a>', reverse('admin:password_change'), _('Cambiar mi contraseña'),
        )

    # ------------------------------------------------------------------ form
    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return self.add_fieldsets
        if obj.pk == request.user.pk:
            return (*self.fieldsets, self.own_password_fieldset)
        return self.fieldsets

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            kwargs['form'] = self.add_form
        form = super().get_form(request, obj, **kwargs)
        form.actor = request.user
        return form

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return ()
        if obj.deleted_at:
            return flatten_fieldsets(self.fieldsets)  # a soft-deleted account is never edited
        readonly = ['username', 'deleted_at', 'last_login', 'date_joined']
        if obj.pk == request.user.pk:
            readonly.append('own_password_link')
        # Only staff roles can change role, and never their own or a superadmin's.
        if obj.role not in STAFF_ROLES or obj.pk == request.user.pk or obj.is_superuser:
            readonly.append('role')
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
        old_role = form.initial.get('role', obj.role)
        if 'email' in form.changed_data and obj.role in (Role.TEACHER, Role.OPERATIONS_STAFF) and obj.email:
            obj.username = obj.email  # same rule as the staff API
        super().save_model(request, obj, form, change)
        if obj.role != old_role:
            apply_role_change(obj, old_role, actor=request.user, request=request)
            self.message_user(request, _('Rol cambiado. Se cerraron las sesiones de la cuenta.'))

    def _create_staff(self, request, obj, form):
        """Goes through the API service so the account gets a temporary password,
        must_change_password and the credentials e-mail like any other staff account."""
        user, temporary_password = create_staff_account(form.cleaned_data, admin=request.user)
        obj.pk = user.pk
        obj.refresh_from_db()
        obj._state.adding = False
        record_audit(
            AuditLog.Action.ACCOUNT_CREATED, actor=request.user, target=user, request=request,
            detail={'role': user.role},
        )
        if notify_staff_credentials(user, temporary_password):
            self.message_user(request, _('Cuenta creada. La contraseña temporal se envió por correo.'))
        else:
            self.message_user(
                request,
                _('Cuenta creada, pero no se pudo enviar el correo. Usa "Restablecer contraseña temporal" más tarde.'),
                messages.WARNING,
            )

    # --------------------------------------------------------------- actions
    def _log(self, request, user, action, message, **detail):
        """The same event in Django's own log (shown in the object's history) and in the audit log."""
        self.log_change(request, user, message)
        record_audit(action, actor=request.user, target=user, request=request, detail=detail)

    @admin.action(description=_('Activar las cuentas seleccionadas'), permissions=['change'])
    def activate_accounts(self, request, queryset):
        done = 0
        for user in queryset.filter(deleted_at__isnull=True, is_active=False):
            user.is_active = True
            user.save(update_fields=['is_active'])
            self._log(request, user, AuditLog.Action.ACCOUNT_ACTIVATED, 'Cuenta activada')
            done += 1
        self.message_user(request, _('%(n)d cuenta(s) activada(s).') % {'n': done})

    @admin.action(description=_('Desactivar las cuentas seleccionadas (cierra sus sesiones)'), permissions=['change'])
    def deactivate_accounts(self, request, queryset):
        targets = queryset.filter(is_active=True, deleted_at__isnull=True, is_superuser=False)
        targets = targets.exclude(pk=request.user.pk)
        done = 0
        for user in targets:
            user.is_active = False
            user.save(update_fields=['is_active'])
            revoke_user_tokens(user)
            self._log(request, user, AuditLog.Action.ACCOUNT_DEACTIVATED, 'Cuenta desactivada')
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
            label = display_name(user)
            soft_delete_account(user)
            revoke_user_tokens(user)
            self.log_change(request, user, f'Cuenta de {label} dada de baja')
            record_audit(
                AuditLog.Action.ACCOUNT_REMOVED, actor=request.user, target=user, request=request,
                detail={'role': user.role},
            )
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
        """Every role. The temporary password goes to the account's e-mail; a student has
        none, so it goes to the parent (with the student's username)."""
        selected = list(queryset.select_related('institution')[:RESET_LIMIT + 1])
        if len(selected) > RESET_LIMIT:
            self.message_user(
                request,
                _('Selecciona como máximo %(n)d cuentas por vez para restablecer su contraseña.') % {'n': RESET_LIMIT},
                messages.ERROR,
            )
            return
        outcomes = Counter()
        for user in selected:
            if user.pk == request.user.pk or not user.is_active or user.deleted_at is not None:
                outcomes['skipped'] += 1
                continue
            outcome = reset_account_password(user, actor=request.user, request=request)
            outcomes[outcome] += 1
            if outcome != 'no_recipient':
                self.log_change(request, user, 'Contraseña temporal restablecida')
        if outcomes['sent']:
            self.message_user(
                request,
                _('%(n)d contraseña(s) restablecida(s): se envió la temporal por correo.') % {'n': outcomes['sent']},
            )
        if outcomes['email_failed']:
            self.message_user(
                request,
                _('%(n)d cuenta(s) cambiaron de contraseña pero el correo no se pudo encolar: vuelve a restablecerlas '
                  'cuando el servicio de correo esté disponible.') % {'n': outcomes['email_failed']},
                messages.ERROR,
            )
        if outcomes['no_recipient']:
            self.message_user(
                request,
                _('%(n)d cuenta(s) sin correo al que enviar la contraseña (no se tocaron).') % {'n': outcomes['no_recipient']},
                messages.WARNING,
            )
        if outcomes['skipped']:
            self.message_user(
                request,
                _('%(n)d cuenta(s) omitidas: tu propia cuenta (usa "Cambiar mi contraseña"), las inactivas y las '
                  'dadas de baja.') % {'n': outcomes['skipped']},
                messages.WARNING,
            )


@admin.register(Institution)
class InstitutionAdmin(admin.ModelAdmin):
    form = InstitutionForm
    list_display = ('name', 'code', 'status')  # extended per request in get_list_display
    list_filter = ('is_active',)
    search_fields = ('name', 'code', 'contact_name', 'contact_email')
    actions = ('reactivate_institutions',)
    readonly_fields = ('suspended_at', 'registration_date')
    fieldsets = (
        (_('Datos de la sede'), {'fields': ('name', 'code', 'address', 'phone')}),
        (_('Contacto'), {'fields': ('contact_name', 'contact_email')}),
        (_('Estado'), {
            'description': _(
                'Una sede suspendida no aparece al registrar estudiantes ni personal nuevo. Las cuentas, '
                'sesiones y ventas que ya existen siguen funcionando.'
            ),
            'fields': ('is_active', 'suspension_reason', 'suspended_at', 'registration_date'),
        }),
    )

    # ------------------------------------------------------------- permissions
    def has_delete_permission(self, request, obj=None):
        """Only a sede nobody depends on (no accounts, students, sales, menus...) can be removed."""
        if obj is None:
            return False
        return not any(
            getattr(obj, relation.get_accessor_name()).exists() for relation in Institution._meta.related_objects
        )

    # --------------------------------------------------------------- list data
    @admin.display(description=_('Estado'), ordering='is_active')
    def status(self, obj):
        return _('Activa') if obj.is_active else _('Suspendida')

    def get_list_display(self, request):
        """The figures are built once per request (a few grouped queries) and read by the
        columns below; nothing is stored on the admin object, which is shared between requests."""
        if not hasattr(request, '_institution_stats'):
            request._institution_stats = institution_stats()
        stats = request._institution_stats

        def column(label, read):
            def render(obj):
                return read(stats[obj.pk])
            render.short_description = label
            return render

        return (
            'name', 'code', 'status',
            column(_('Personal activo'), lambda s: s['staff']),
            column(_('Estudiantes'), lambda s: s['students']),
            column(_('Saldo de billeteras'), lambda s: f"$ {s['balance']}"),
            column(_('Ventas del mes'), lambda s: f"$ {s['month_sales']} ({s['month_count']})"),
            column(_('Última venta'), lambda s: s['last_sale'] or '—'),
        )

    # ------------------------------------------------------------------- save
    def save_model(self, request, obj, form, change):
        if not change:
            super().save_model(request, obj, form, change)
            record_audit(AuditLog.Action.INSTITUTION_UPDATED, actor=request.user, target=obj, request=request,
                         detail={'created': True})
            return
        was_active = form.initial.get('is_active', True)
        reason = obj.suspension_reason
        wants_active = obj.is_active
        obj.is_active = was_active  # the services below own the suspension fields
        obj.suspension_reason = form.initial.get('suspension_reason', '')
        edited = [f for f in form.changed_data if f not in ('is_active', 'suspension_reason')]
        if edited:
            super().save_model(request, obj, form, change)
            record_audit(AuditLog.Action.INSTITUTION_UPDATED, actor=request.user, target=obj, request=request,
                         detail={'fields': edited})
        if was_active and not wants_active:
            suspend_institution(obj, reason, actor=request.user, request=request)
            self.message_user(request, _('Sede suspendida. Ya no aparece para nuevos registros.'))
        elif not was_active and wants_active:
            reactivate_institution(obj, actor=request.user, request=request)
            self.message_user(request, _('Sede reactivada.'))
        elif not was_active and 'suspension_reason' in form.changed_data:
            obj.suspension_reason = reason[:255]
            obj.save(update_fields=['suspension_reason'])

    @admin.action(description=_('Reactivar las sedes seleccionadas'), permissions=['change'])
    def reactivate_institutions(self, request, queryset):
        done = 0
        for institution in queryset.filter(is_active=False):
            reactivate_institution(institution, actor=request.user, request=request)
            self.log_change(request, institution, 'Sede reactivada')
            done += 1
        self.message_user(request, _('%(n)d sede(s) reactivada(s).') % {'n': done})


@admin.register(ParentProfile)
class ParentProfileAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('parent_name', 'parent_email', 'children_total', 'has_payment_pin')
    search_fields = ('user__username', 'user__email', 'user__first_name', 'user__last_name')
    list_select_related = ('user',)
    exclude = ('payment_pin_hash',)

    def get_queryset(self, request):
        from django.db.models import Count
        return super().get_queryset(request).annotate(children_count=Count('children'))

    @admin.display(description=_('Padre'), ordering='user__last_name')
    def parent_name(self, obj):
        return display_name(obj.user)

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
        return display_name(obj.user)

    @admin.display(description=_('Usuario'), ordering='user__username')
    def username(self, obj):
        return obj.user.username

    @admin.display(description=_('Padre'), ordering='parent__user__last_name')
    def parent_name(self, obj):
        return display_name(obj.parent.user)

    @admin.display(description=_('Saldo'))
    def wallet_balance(self, obj):
        try:
            return f'$ {obj.wallet.balance}'
        except ObjectDoesNotExist:
            return _('Sin billetera')


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Who did what and when: from the app, the superadmin and the system. Review only."""

    list_display = ('created_at', 'action', 'actor_label', 'target_label', 'institution', 'source', 'ip')
    list_filter = ('action', 'source', 'institution')
    search_fields = ('actor_label', 'target_label', 'ip')
    date_hierarchy = 'created_at'
    list_select_related = ('institution',)
    fields = (
        'created_at', 'action', 'source', 'actor_label', 'target_type', 'target_id', 'target_label',
        'institution', 'ip', 'detail',
    )
