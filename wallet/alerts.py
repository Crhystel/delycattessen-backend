"""Alerts about stuck payments for the superadmin index (the table is not listed there)."""
from datetime import timedelta

from django.utils import timezone

from core.alerts import WARNING, Alert, register_alert

from .models import Transaction

STUCK_MINUTES = 30


@register_alert
def pending_recharge_alerts():
    count = Transaction.objects.filter(
        type=Transaction.Type.RECHARGE, status=Transaction.Status.PENDING,
        created_at__lt=timezone.now() - timedelta(minutes=STUCK_MINUTES),
    ).count()
    if count:
        yield Alert(WARNING, f'Recargas pendientes hace más de {STUCK_MINUTES} minutos (la pasarela no confirmó)', count,
                    hint='Si el padre ya pagó, revisa la pasarela')
