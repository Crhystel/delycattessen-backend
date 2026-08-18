from decimal import Decimal

from celery import shared_task

from .models import Wallet
from .services import GatewayError, GatewayRouter


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def process_recharge_task(
    self,
    wallet_id: int,
    amount: str,
    token: str = None,
    document_number: str = None,
    phone_number: str = None,
) -> None:
    wallet = Wallet.objects.get(pk=wallet_id)
    gateway = GatewayRouter().get_gateway(Decimal(amount))
    try:
        gateway.process_recharge(
            wallet, Decimal(amount), token=token,
            document_number=document_number, phone_number=phone_number,
        )
    except GatewayError as exc:
        raise self.retry(exc=exc)