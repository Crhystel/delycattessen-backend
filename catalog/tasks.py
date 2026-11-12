import html
import io
import logging
import re
from datetime import date
from email.message import MIMEPart
from typing import Iterable
from django.conf import settings
from django.core.mail import get_connection, EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from celery import shared_task
from core.timezone import local_today
from .models import Combo, MonthlyMenu

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

EMAIL_BATCH_SIZE = 50
COMBO_IMAGE_CID = 'combo-image'


def _parents_with_children_in(institution):
    """Active parents with an email and at least one child in `institution`."""
    from users.models import CustomUser

    return (
        CustomUser.objects.filter(
            role=CustomUser.Role.PARENT,
            is_active=True,
            parent_profile__children__institution=institution,
        )
        .exclude(email='')
        .exclude(email__isnull=True)
        .distinct()
    )


def _from_email() -> str:
    return (
        settings.DEFAULT_FROM_EMAIL
        if hasattr(settings, 'DEFAULT_FROM_EMAIL') and settings.DEFAULT_FROM_EMAIL
        else settings.EMAIL_HOST_USER
    )


def _send_in_batches(connection, messages: Iterable[EmailMultiAlternatives]) -> int:
    """Sends `messages` through an open connection, EMAIL_BATCH_SIZE at a time.
    Returns how many were sent."""
    batch = []
    total_sent = 0
    for message in messages:
        batch.append(message)
        if len(batch) >= EMAIL_BATCH_SIZE:
            connection.send_messages(list(batch))
            total_sent += len(batch)
            batch.clear()
    if batch:
        connection.send_messages(list(batch))
        total_sent += len(batch)
        batch.clear()
    return total_sent


def _close_quietly(connection) -> None:
    if connection:
        try:
            connection.close()
        except Exception:
            pass


class RelatedImageEmail(EmailMultiAlternatives):
    """Django always nests attachments in multipart/mixed; inline images
    referenced by cid: belong in multipart/related so clients show them in the body."""

    def message(self, *args, **kwargs):
        message = super().message(*args, **kwargs)
        if message.get_content_type() == 'multipart/mixed':
            message.replace_header('Content-Type', 'multipart/related')
        return message


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def send_monthly_menu_published_email(self, monthly_menu_id: int) -> None:
    """
    Sends an individual HTML + plain text email notification using the
    monthly_menu_published.html template to every parent who has active
    children in the published monthly menu's institution.
    Reuses a single SMTP connection for efficiency.
    """
    try:
        menu = MonthlyMenu.objects.select_related('institution').get(pk=monthly_menu_id)
    except MonthlyMenu.DoesNotExist:
        logger.warning(f"MonthlyMenu with id={monthly_menu_id} does not exist. Aborting email task.")
        return

    parents = _parents_with_children_in(menu.institution)

    if not parents.exists():
        logger.info(f"No parents found for institution {menu.institution.name}. No emails sent.")
        return

    month_name = MONTHS_ES.get(menu.month, str(menu.month))
    subject = "Nuevo menú mensual disponible en Snack-Up"
    from_email = _from_email()

    def build_messages(connection):
        for parent in parents.iterator(chunk_size=EMAIL_BATCH_SIZE):
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
            yield email_message

    connection = None
    try:
        connection = get_connection()
        connection.open()
        total_sent = _send_in_batches(connection, build_messages(connection))
        logger.info(f"Sent {total_sent} monthly menu notification emails for menu {monthly_menu_id}.")
    except Exception as exc:
        logger.error(f"Error sending monthly menu published emails: {exc}")
        raise self.retry(exc=exc)
    finally:
        _close_quietly(connection)


def _long_date(day: date, with_year: bool = True) -> str:
    text = f"{day.day} de {MONTHS_ES[day.month].lower()}"
    return f"{text} de {day.year}" if with_year else text


def format_validity(start: date, end: date, today: date) -> str:
    """Spanish validity sentence of an offer, e.g. "Válido del 12 al 18 de
    noviembre de 2026"; an offer that has not started says "Disponible desde"."""
    if start > today:
        if start == end:
            return f"Disponible el {_long_date(start)}"
        start_text = _long_date(start, with_year=start.year != end.year)
        return f"Disponible desde el {start_text} hasta el {_long_date(end)}"
    if start == end:
        return f"Válido el {_long_date(start)}"
    if (start.year, start.month) == (end.year, end.month):
        return f"Válido del {start.day} al {_long_date(end)}"
    start_text = _long_date(start, with_year=start.year != end.year)
    return f"Válido del {start_text} al {_long_date(end)}"


def _read_combo_image(combo: Combo) -> tuple[bytes, str] | None:
    """Bytes and MIME subtype of the combo image, or None when it cannot be read."""
    # Imported here: PIL.Image pulls numpy in, which must stay out of the web process at startup.
    from PIL import Image

    try:
        with combo.image.open('rb') as image_file:
            data = image_file.read()
        with Image.open(io.BytesIO(data)) as image:
            subtype = image.format.lower()
    except (OSError, ValueError, AttributeError):
        logger.warning(f"Combo {combo.pk} image could not be read; sending the email without it.")
        return None
    return data, subtype


def _inline_image_part(data: bytes, subtype: str) -> MIMEPart:
    """A new part for each message: one part object is not safe to share."""
    part = MIMEPart()
    part.set_content(
        data, maintype='image', subtype=subtype, disposition='inline', cid=f'<{COMBO_IMAGE_CID}>',
    )
    return part


def _plain_text(html_content: str) -> str:
    text = html.unescape(strip_tags(html_content))
    lines = [line.strip() for line in text.splitlines()]
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def send_combo_created_email(self, combo_id: int) -> None:
    """
    Emails every parent with a child in the combo's institution about the new
    combo (combo_created.html, HTML + plain text). The combo image travels
    inside the message because mail clients block remote images.
    """
    try:
        combo = (
            Combo.objects.select_related('institution')
            .prefetch_related('items__menu_item')
            .get(pk=combo_id)
        )
    except Combo.DoesNotExist:
        logger.warning(f"Combo with id={combo_id} does not exist. Aborting email task.")
        return

    parents = _parents_with_children_in(combo.institution)

    if not parents.exists():
        logger.info(f"No parents found for institution {combo.institution.name}. No emails sent.")
        return

    image = _read_combo_image(combo)
    shared_context = {
        'combo_name': combo.name,
        'items': [{'quantity': item.quantity, 'name': item.menu_item.name} for item in combo.items.all()],
        'validity': format_validity(combo.start_date, combo.end_date, local_today()),
        'has_image': image is not None,
    }
    subject = f"Nuevo combo disponible en Snack-Up: {combo.name}"
    from_email = _from_email()

    def build_messages(connection):
        for parent in parents.iterator(chunk_size=EMAIL_BATCH_SIZE):
            parent_name = f"{parent.first_name} {parent.last_name}".strip() or parent.username
            html_content = render_to_string(
                'emails/combo_created.html', {**shared_context, 'parent_name': parent_name},
            )
            email_message = RelatedImageEmail(
                subject=subject,
                body=_plain_text(html_content),
                from_email=from_email,
                to=[parent.email],
                connection=connection,
            )
            email_message.attach_alternative(html_content, 'text/html')
            if image:
                email_message.attach(_inline_image_part(*image))
            yield email_message

    connection = None
    try:
        connection = get_connection()
        connection.open()
        total_sent = _send_in_batches(connection, build_messages(connection))
        logger.info(f"Sent {total_sent} new combo notification emails for combo {combo_id}.")
    except Exception as exc:
        logger.error(f"Error sending new combo emails: {exc}")
        raise self.retry(exc=exc)
    finally:
        _close_quietly(connection)
