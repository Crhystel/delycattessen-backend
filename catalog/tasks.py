import logging
from django.conf import settings
from django.core.mail import get_connection, EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags
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
    Sends an individual HTML + plain text email notification using the
    monthly_menu_published.html template to every parent who has active
    children in the published monthly menu's institution.
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
    subject = "Nuevo menú mensual disponible en Snack-Up"
    from_email = (
        settings.DEFAULT_FROM_EMAIL
        if hasattr(settings, 'DEFAULT_FROM_EMAIL') and settings.DEFAULT_FROM_EMAIL
        else settings.EMAIL_HOST_USER
    )

    connection = None
    try:
        connection = get_connection()
        connection.open()
        batch = []
        total_sent = 0
        for parent in parents.iterator(chunk_size=50):
            parent_name = f"{parent.first_name} {parent.last_name}".strip() or parent.username
            context = {
                'parent_name': parent_name,
                'month_name': month_name,
                'year': menu.year,
                'institution_name': menu.institution.name,
            }
            html_content = render_to_string('emails/monthly_menu_published.html', context)
            text_content = strip_tags(html_content)

            email_message = EmailMultiAlternatives(
                subject=subject,
                body=text_content,
                from_email=from_email,
                to=[parent.email],
                connection=connection,
            )
            email_message.attach_alternative(html_content, 'text/html')
            batch.append(email_message)

            if len(batch) >= 50:
                connection.send_messages(list(batch))
                total_sent += len(batch)
                batch.clear()

        if batch:
            connection.send_messages(list(batch))
            total_sent += len(batch)
            batch.clear()

        logger.info(f"Sent {total_sent} monthly menu notification emails for menu {monthly_menu_id}.")
    except Exception as exc:
        logger.error(f"Error sending monthly menu published emails: {exc}")
        raise self.retry(exc=exc)
    finally:
        if connection:
            try:
                connection.close()
            except Exception:
                pass
