from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.translation import gettext_lazy as _


@shared_task
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