from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string


@shared_task
def send_password_reset_email(email: str, code: str) -> None:
    """
    Asynchronous task (Celery/RabbitMQ): sends the password reset
    verification code to the user's email using the HTML template.
    """
    subject = 'Código de recuperación de contraseña - D\'Elycattessen'
    text_body = (
        f'Tu código de verificación es: {code}\n\n'
        f'Este código es de un solo uso y vence en 15 minutos.'
    )
    html_body = render_to_string('emails/password_reset.html', {'code': code})

    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[email],
    )
    message.attach_alternative(html_body, 'text/html')
    message.send()