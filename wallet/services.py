import uuid
from abc import ABC, abstractmethod
from decimal import Decimal

from django.db import transaction
import requests
from django.conf import settings
import urllib

from .models import Transaction, Wallet


class GatewayError(Exception):
    pass


class PaymentGateway(ABC):
    """Template Method: fixes the recharge flow (validate -> pending record ->
    charge -> confirm -> credit balance). Subclasses only implement the
    gateway-specific request step (Open/Closed)."""

    gateway_code: str = None

    def process_recharge(self, wallet: Wallet, amount: Decimal) -> Transaction:
        self._validate_amount(amount)
        transaction = self._create_pending_transaction(wallet, amount)
        try:
            external_id = self._send_charge_request(wallet, amount)
        except GatewayError:
            transaction.status = Transaction.Status.FAILED
            transaction.save(update_fields=['status'])
            raise
        transaction.external_transaction_id = external_id
        transaction.status = Transaction.Status.SUCCESS
        transaction.save(update_fields=['external_transaction_id', 'status'])
        self._credit_balance(wallet, amount)
        return transaction

    def _validate_amount(self, amount: Decimal) -> None:
        if amount <= 0:
            raise ValueError('Recharge amount must be greater than zero.')

    def _create_pending_transaction(self, wallet: Wallet, amount: Decimal) -> Transaction:
        return Transaction.objects.create(
            wallet=wallet,
            amount=amount,
            gateway=self.gateway_code,
            status=Transaction.Status.PENDING,
            type=Transaction.Type.RECHARGE,
        )

    def _credit_balance(self, wallet: Wallet, amount: Decimal) -> None:
        wallet.balance += amount
        wallet.save(update_fields=['balance'])

    @abstractmethod
    def _send_charge_request(self, wallet: Wallet, amount: Decimal) -> str:
        raise NotImplementedError


class KushkiGateway(PaymentGateway):
    gateway_code = Transaction.Gateway.KUSHKI

    def _send_charge_request(self, wallet: Wallet, amount: Decimal) -> str:
        raise NotImplementedError('Kushki integration pending — no sandbox credentials yet.')


class PayphoneGateway(PaymentGateway):
    gateway_code = Transaction.Gateway.PAYPHONE

    def _send_charge_request(self, wallet: Wallet, amount: Decimal) -> str:
        raise NotImplementedError('Payphone real usa PayphonePreparer, no este flujo síncrono.')


class FakeKushkiGateway(PaymentGateway):

    gateway_code = Transaction.Gateway.KUSHKI

    def _send_charge_request(self, wallet: Wallet, amount: Decimal) -> str:
        if amount == Decimal('4.44'): 
            raise GatewayError('Simulated Kushki decline for testing.')
        return f'fake-kushki-{uuid.uuid4().hex[:12]}'


class FakePayphoneGateway(PaymentGateway):
    gateway_code = Transaction.Gateway.PAYPHONE

    def _send_charge_request(self, wallet: Wallet, amount: Decimal) -> str:
        if amount == Decimal('4.44'):
            raise GatewayError('Simulated Payphone decline for testing.')
        return f'fake-payphone-{uuid.uuid4().hex[:12]}'


class GatewayRouter:

    def get_gateway(self, amount: Decimal) -> PaymentGateway:
        threshold = settings.WALLET_RECHARGE_GATEWAY_THRESHOLD
        use_fake = settings.WALLET_USE_FAKE_GATEWAYS

        if amount < threshold:
            return FakePayphoneGateway() if use_fake else PayphoneGateway()
        return FakeKushkiGateway() if use_fake else KushkiGateway()


class PayphonePreparer:
    def prepare(self, wallet: Wallet, amount: Decimal) -> dict:
        transaction = Transaction.objects.create(
            wallet=wallet,
            amount=amount,
            gateway=Transaction.Gateway.PAYPHONE,
            status=Transaction.Status.PENDING,
            type=Transaction.Type.RECHARGE,
        )
        response = requests.post(
            f'{settings.PAYPHONE_API_URL}/button/Prepare',
            json={
                'amount': int(amount * 100),
                'amountWithoutTax': int(amount * 100),
                'clientTransactionId': str(transaction.id),
                'currency': 'USD',
                'storeId': settings.PAYPHONE_STORE_ID,
                'reference': f'Recarga billetera #{wallet.id}',
                'responseUrl': settings.PAYPHONE_RESPONSE_URL,
            },
            headers={
                'Authorization': f'Bearer {settings.PAYPHONE_TOKEN}',
                'Content-Type': 'application/json',
            },
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        print('PAYPHONE PREPARE STATUS:', response.status_code)
        print('PAYPHONE PREPARE RESPONSE:', data)

        payment_url = data['payWithCard']
        redirect_url = (
            f"{settings.PAYPHONE_REDIRECT_BASE_URL}?target={urllib.parse.quote(payment_url, safe='')}"
        )

        transaction.external_transaction_id = str(data['paymentId'])
        transaction.save(update_fields=['external_transaction_id'])

        return {
            'transaction_id': transaction.id,
            'payment_url': redirect_url,
        }

    def confirm(self, payphone_id: int, client_transaction_id: str) -> Transaction:
        transaction = Transaction.objects.select_related('wallet').get(pk=client_transaction_id)

        response = requests.post(
            f'{settings.PAYPHONE_API_URL}/button/V2/Confirm',
            json={'id': payphone_id, 'clientTxId': client_transaction_id},
            headers={
                'Authorization': f'Bearer {settings.PAYPHONE_TOKEN}',
                'Content-Type': 'application/json',
            },
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()

        if data.get('transactionStatus') == 'Approved':
            transaction.status = Transaction.Status.SUCCESS
            transaction.wallet.balance += transaction.amount
            transaction.wallet.save(update_fields=['balance'])
        else:
            transaction.status = Transaction.Status.FAILED
        transaction.save(update_fields=['status'])
        return transaction