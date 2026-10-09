import logging
from django.conf import settings
from django.core.mail import get_connection, EmailMessage
from celery import shared_task
from .models import MonthlyMenu

logger = logging.getLogger(__name__)

MONTHS_ES = {
    1: 'Enero',
    2: 'Febrero',
    3: 'Marzo',
    4: 'Abril',
    5: 'Mayo',
    6: 'Junio',
    7: 'Julio',
    8: 'Agosto',
    9: 'Septiembre',
    10: 'Octubre',
    11: 'Noviembre',
    12: 'Diciembre',
}


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def send_monthly_menu_published_email(self, monthly_menu_id: int) -> None:
    """
    Sends an individual plain text email notification to every parent
    who has active children in the published monthly menu's institution.
    Reuses a single SMTP connection for efficiency.
    """
    from users.models import CustomUser

    try:
        menu = MonthlyMenu.objects.select_related('institution').get(pk=monthly_menu_id)
    except MonthlyMenu.DoesNotExist:
        logger.warning(f"MonthlyMenu with id={monthly_menu_id} does not exist. Aborting email task.")
        return

    # Destinatarios: Padres activos con al menos un hijo en la institución del menú
    parents = (
        CustomUser.objects.filter(
            role=CustomUser.Role.PARENT,
            is_active=True,
            parent_profile__children__institution=menu.institution,
        )
        .exclude(email='')
        .exclude(email__isnull=True)
        .distinct()
    )

    if not parents.exists():
        logger.info(f"No parents found for institution {menu.institution.name}. No emails sent.")
        return

    month_name = MONTHS_ES.get(menu.month, str(menu.month))
    subject = "Nuevo menú mensual publicado - D'Elycattessen"

    try:
        connection = get_connection()
        messages = []
        for parent in parents:
            parent_name = f"{parent.first_name} {parent.last_name}".strip() or parent.username
            body = (
                f"Estimado/a {parent_name},\n\n"
                f"Le informamos que ya se encuentra publicado el menú mensual correspondiente a "
                f"{month_name} {menu.year} para la institución {menu.institution.name}.\n\n"
                f"Ya puede ingresar a la aplicación móvil D'Elycattessen para revisar las opciones disponibles "
                f"y realizar las preórdenes de alimentación para sus hijos.\n\n"
                f"Atentamente,\n"
                f"Equipo D'Elycattessen"
            )
            message = EmailMessage(
                subject=subject,
                body=body,
                from_email=settings.DEFAULT_FROM_EMAIL if hasattr(settings, 'DEFAULT_FROM_EMAIL') and settings.DEFAULT_FROM_EMAIL else settings.EMAIL_HOST_USER,
                to=[parent.email],
                connection=connection,
            )
            messages.append(message)

        if messages:
            connection.send_messages(messages)
            logger.info(f"Sent {len(messages)} monthly menu notification emails for menu {monthly_menu_id}.")
    except Exception as exc:
        logger.error(f"Error sending monthly menu published emails: {exc}")
        raise self.retry(exc=exc)
