from smtplib import SMTPException

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.translation import gettext_lazy as _


@shared_task(acks_late=True, reject_on_worker_lost=True)
def send_password_reset_email(email: str, code: str) -> None:
    """
    Asynchronous task (Celery/RabbitMQ): sends the password reset
    verification code to the user's email using the HTML template.
    """
    subject = str(_('Código de recuperación de contraseña - D\'Elycattessen'))
    text_body = str(_('Tu código de verificación es: %(code)s\n\nEste código es de un solo uso y vence en 15 minutos.')) % {'code': code}
    html_body = render_to_string('emails/password_reset.html', {'code': code})

    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[email],
    )
    message.attach_alternative(html_body, 'text/html')
    message.send()


@shared_task(
    autoretry_for=(SMTPException, OSError), retry_backoff=True, max_retries=3,
    acks_late=True, reject_on_worker_lost=True,
)
def send_staff_credentials_email(email: str, first_name: str, role_label: str, temporary_password: str) -> None:
    """
    Asynchronous task (Celery/RabbitMQ): sends the temporary password to the
    new teacher / operations-staff account. The password is single-use (the
    user must change it on first login) and is never logged or stored as a task
    result (CELERY_TASK_IGNORE_RESULT). Retried only on delivery failures.
    """
    context = {
        'first_name': first_name,
        'role_label': role_label,
        'email': email,
        'temporary_password': temporary_password,
    }
    subject = str(_('Tus credenciales de acceso - D\'Elycattessen'))
    text_body = str(_(
        'Hola, %(first_name)s\n\n'
        'Se creó tu cuenta de %(role_label)s en D\'Elycattessen.\n'
        'Usuario: %(email)s\n'
        'Contraseña temporal: %(temporary_password)s\n\n'
        'Deberás cambiar esta contraseña en tu primer inicio de sesión.'
    )) % context
    html_body = render_to_string('emails/staff_credentials.html', context)

    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[email],
    )
    message.attach_alternative(html_body, 'text/html')
    message.send()


@shared_task(
    autoretry_for=(SMTPException, OSError), retry_backoff=True, max_retries=3,
    acks_late=True, reject_on_worker_lost=True,
)
def send_password_reset_by_admin_email(
    email: str, first_name: str, account_label: str, login: str, temporary_password: str,
) -> None:
    """
    Asynchronous task (Celery/RabbitMQ): sends the temporary password that the
    superadmin set for an account. For a student it goes to the parent's e-mail, so
    `account_label` names the account and `login` is what the person types to sign in.
    The password is single-use, never logged and never kept as a task result.
    """
    context = {
        'first_name': first_name,
        'account_label': account_label,
        'login': login,
        'temporary_password': temporary_password,
    }
    subject = str(_("Nueva contraseña temporal - D'Elycattessen"))
    text_body = str(_(
        'Hola, %(first_name)s\n\n'
        "El administrador restableció la contraseña de la cuenta de %(account_label)s en D'Elycattessen.\n"
        'Usuario: %(login)s\n'
        'Contraseña temporal: %(temporary_password)s\n\n'
        'Deberá cambiar esta contraseña en su próximo inicio de sesión.'
    )) % context
    html_body = render_to_string('emails/admin_password_reset.html', context)

    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[email],
    )
    message.attach_alternative(html_body, 'text/html')
    message.send()


@shared_task(
    autoretry_for=(SMTPException, OSError), retry_backoff=True, max_retries=3,
    acks_late=True, reject_on_worker_lost=True,
)
def send_child_password_changed_email(email: str, first_name: str, child_name: str, child_username: str) -> None:
    """
    Asynchronous task (Celery/RabbitMQ): tells the parent that the password of
    their child's account was changed. The notice never carries the password.
    """
    context = {'first_name': first_name, 'child_name': child_name, 'child_username': child_username}
    subject = str(_("Cambió la contraseña de tu hijo/a - D'Elycattessen"))
    text_body = str(_(
        'Hola, %(first_name)s\n\n'
        "Se cambió la contraseña de la cuenta de %(child_name)s (usuario: %(child_username)s) en D'Elycattessen.\n\n"
        'Si no fuiste tú, cambia tu contraseña y comunícate con la institución.'
    )) % context
    html_body = render_to_string('emails/child_password_changed.html', context)

    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[email],
    )
    message.attach_alternative(html_body, 'text/html')
    message.send()
