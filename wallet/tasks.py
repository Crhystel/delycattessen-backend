from decimal import Decimal

from celery import shared_task

from .models import Wallet
from .services import GatewayError, GatewayRouter


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def process_recharge_task(self, wallet_id: int, amount: str) -> None:
    wallet = Wallet.objects.get(pk=wallet_id)
    gateway = GatewayRouter().get_gateway(Decimal(amount))
    try:
        gateway.process_recharge(wallet, Decimal(amount))
    except GatewayError as exc:
        raise self.retry(exc=exc)