"""Alerts about report exports and credit statements for the superadmin index.
The tables themselves are not in the superadmin, so these alerts carry no link."""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from core.alerts import ERROR, WARNING, Alert, register_alert

from .models import CreditStatement, ReportExport


@register_alert
def report_alerts():
    now = timezone.now()
    count = ReportExport.objects.filter(status=ReportExport.Status.FAILED, created_at__gte=now - timedelta(hours=24)).count()
    if count:
        yield Alert(ERROR, 'Reportes exportados que fallaron en las últimas 24 horas', count,
                    hint='El administrador puede volver a pedirlos desde el panel web')
    stuck_since = now - timedelta(seconds=settings.REPORT_EXPORT_TIMEOUT_SECONDS)
    count = ReportExport.objects.filter(status__in=ReportExport.ACTIVE_STATUSES, created_at__lt=stuck_since).count()
    if count:
        yield Alert(WARNING, 'Reportes atascados en cola o generándose (¿el worker de Celery está activo?)', count)


@register_alert
def credit_statement_alerts():
    count = CreditStatement.objects.filter(status=CreditStatement.Status.FAILED).count()
    if count:
        yield Alert(ERROR, 'Estados de cuenta a docentes que no se pudieron enviar', count,
                    hint='Se reenvían desde el reporte de consumo docente del panel web')
    count = CreditStatement.objects.filter(
        status=CreditStatement.Status.QUEUED, created_at__lt=timezone.now() - timedelta(hours=1),
    ).count()
    if count:
        yield Alert(WARNING, 'Estados de cuenta en cola desde hace más de una hora', count)
