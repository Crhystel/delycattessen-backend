from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.utils.translation import gettext as _

from core import health


class Command(BaseCommand):
    """
    Emails the superadmins when no Celery worker listens to the queue (recharges,
    e-mails and report exports stop meanwhile). Meant to run every few minutes
    from cron or the Windows Task Scheduler; the mail is sent directly over SMTP
    because Celery is precisely what is down.
    """
    help = 'Avisa por correo a los superadministradores si no hay workers de Celery.'

    def handle(self, *args, **options):
        check = health.check_workers()
        if check.status != health.ERROR:
            self.stdout.write(f'OK: {check.detail}')
            return

        recipients = list(
            get_user_model().objects.filter(is_superuser=True, is_active=True)
            .exclude(email__isnull=True).exclude(email='')
            .values_list('email', flat=True)
        )
        if not recipients:
            self.stderr.write(f'Sin workers ({check.detail}) y ningún superadministrador con correo.')
            return

        send_mail(
            subject=_("Alerta: los workers de Celery no están funcionando - D'Elycattessen"),
            message=_(
                'El sistema detectó un problema con los workers de Celery:\n\n%(detail)s\n\n'
                'Mientras no se reinicien, las recargas con tarjeta fallan al vencer su tiempo de '
                'espera, y los correos y los reportes en Excel o PDF quedan en cola. '
                'Reinicia el worker y revisa "Estado del sistema" en el panel de administración.'
            ) % {'detail': check.detail},
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=recipients,
        )
        self.stdout.write(self.style.WARNING(f'Alerta enviada a {len(recipients)} superadministrador(es): {check.detail}'))
