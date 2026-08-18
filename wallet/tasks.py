from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from celery import shared_task

from .models import Wallet


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def process_recharge_task(self, wallet_id: int, amount: str, token: str = None,
                            document_number: str = None, phone_number: str = None) -> None:
    from decimal import Decimal
    from .services import GatewayError, GatewayRouter

    wallet = Wallet.objects.get(pk=wallet_id)
    gateway = GatewayRouter().get_gateway(Decimal(amount))
    try:
        gateway.process_recharge(
            wallet, Decimal(amount), token=token,
            document_number=document_number, phone_number=phone_number,
        )
    except GatewayError as exc:
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def send_recharge_confirmation_email(self, transaction_id: int) -> None:
    """Sends a confirmation email after a successful recharge."""
    from .models import Transaction  # local import to avoid circular import at module load

    transaction = Transaction.objects.select_related('wallet__student__parent__user').get(pk=transaction_id)
    parent_user = transaction.wallet.student.parent.user

    if not parent_user.email:
        return  # nothing to send to

    context = {
        'parent_first_name': parent_user.first_name,
        'parent_last_name': parent_user.last_name,
        'child_name': transaction.wallet.student.user.first_name,
        'amount': transaction.amount,
        'gateway': transaction.get_gateway_display(),
        'ticket_number': transaction.external_transaction_id,
        'reference': f'Recarga billetera #{transaction.wallet.id}',
        'date': transaction.created_at,
    }
    html_content = render_to_string('emails/recharge_confirmation.html', context)
    text_content = strip_tags(html_content)

    email = EmailMultiAlternatives(
        subject="Confirmación de recarga - D'Elycattessen",
        body=text_content,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[parent_user.email],
    )
    email.attach_alternative(html_content, 'text/html')
    email.send()