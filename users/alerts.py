"""Alerts about accounts, sedes and logins for the superadmin index (see core/alerts.py)."""
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Q
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from core.alerts import ERROR, WARNING, Alert, register_alert

from .admin_services import STAFF_ROLES
from .models import AuditLog, CustomUser, Institution, ParentProfile, StudentProfile

Role = CustomUser.Role
PENDING_ACTIVATION_DAYS = 7


def _url(name, query=''):
    try:
        return reverse(name) + query
    except NoReverseMatch:
        return ''


@register_alert
def account_alerts():
    accounts = CustomUser.objects.filter(deleted_at__isnull=True)
    users_url = 'admin:users_customuser_changelist'

    count = accounts.filter(role=Role.STUDENT, student_profile__isnull=True).count()
    if count:
        yield Alert(ERROR, 'Estudiantes sin perfil: no pueden usar la billetera ni el menú', count,
                    _url(users_url, '?sin_perfil=estudiante'))
    count = accounts.filter(role=Role.PARENT, parent_profile__isnull=True).count()
    if count:
        yield Alert(ERROR, 'Padres sin perfil: fallan varias pantallas de la app', count,
                    _url(users_url, '?sin_perfil=padre'))
    count = StudentProfile.objects.filter(user__deleted_at__isnull=True, wallet__isnull=True).count()
    if count:
        yield Alert(ERROR, 'Estudiantes sin billetera: no se les puede cobrar ni recargar', count,
                    _url('admin:users_studentprofile_changelist'))
    count = accounts.filter(role__in=STAFF_ROLES, institution__isnull=True).count()
    if count:
        yield Alert(WARNING, 'Cuentas de personal sin institución (no pueden operar)', count, _url(users_url))

    limit = timezone.now() - timedelta(days=PENDING_ACTIVATION_DAYS)
    count = accounts.filter(
        role__in=(Role.TEACHER, Role.OPERATIONS_STAFF), is_active=True, must_change_password=True,
        date_joined__lt=limit,
    ).count()
    if count:
        yield Alert(WARNING, f'Cuentas con contraseña temporal sin usar hace más de {PENDING_ACTIVATION_DAYS} días',
                    count, _url(users_url))


@register_alert
def institution_alerts():
    no_admin = Institution.objects.filter(is_active=True).annotate(
        admins=Count('users', filter=Q(users__role=Role.ADMIN, users__is_active=True, users__deleted_at__isnull=True)),
    ).filter(admins=0).count()
    if no_admin:
        yield Alert(WARNING, 'Sedes activas sin ningún administrador', no_admin,
                    _url('admin:users_institution_changelist'))

    several = ParentProfile.objects.annotate(
        sedes=Count('children__institution', filter=Q(children__user__deleted_at__isnull=True), distinct=True),
    ).filter(sedes__gt=1).count()
    if several:
        yield Alert(WARNING, 'Padres con hijos en más de una institución (la app lo impide al registrar)', several,
                    _url('admin:users_parentprofile_changelist'))


@register_alert
def security_alerts():
    from axes.models import AccessAttempt

    window = timezone.now() - settings.AXES_COOLOFF_TIME
    blocked = AccessAttempt.objects.filter(
        failures_since_start__gte=settings.AXES_FAILURE_LIMIT, attempt_time__gte=window,
    ).count()
    if blocked:
        yield Alert(WARNING, 'Inicios de sesión bloqueados por demasiados intentos fallidos', blocked,
                    _url('admin:axes_accessattempt_changelist'),
                    'Eliminar el registro lo desbloquea')

    failed = AuditLog.objects.filter(
        action=AuditLog.Action.TASK_FAILED, created_at__gte=timezone.now() - timedelta(hours=24),
    ).count()
    if failed:
        yield Alert(ERROR, 'Tareas en segundo plano fallidas en las últimas 24 horas (correos, cobros, reportes)',
                    failed, _url('admin:users_auditlog_changelist', '?action__exact=TASK_FAILED'))
