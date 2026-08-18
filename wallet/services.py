import urllib.parse
from abc import ABC, abstractmethod
from decimal import Decimal

import requests
from django.conf import settings

from .models import Transaction, Wallet


class GatewayError(Exception):
    """Raised when an external payment gateway rejects or fails a charge request."""


class PaymentGateway(ABC):
    """Template Method: fixes the recharge flow (validate -> pending record ->
    charge -> confirm -> credit balance). Subclasses only implement the
    gateway-specific request step (Open/Closed)."""

    gateway_code: str = None

    def process_recharge(
        self, wallet: Wallet, amount: Decimal, token: str = None,
        document_number: str = None, phone_number: str = None,
    ) -> Transaction:
        self._validate_amount(amount)
        transaction = self._create_pending_transaction(wallet, amount)
        try:
            external_id = self._send_charge_request(
                wallet, amount, token=token,
                document_number=document_number, phone_number=phone_number,
            )
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
    def _send_charge_request(
        self, wallet: Wallet, amount: Decimal, token: str = None,
        document_number: str = None, phone_number: str = None,
    ) -> str:
        """Send the charge to the external gateway and return its external
        transaction id. Must raise GatewayError on failure."""
        raise NotImplementedError


class KushkiGateway(PaymentGateway):
    gateway_code = Transaction.Gateway.KUSHKI

    def _send_charge_request(
        self, wallet: Wallet, amount: Decimal, token: str = None,
        document_number: str = None, phone_number: str = None,
    ) -> str:
        if not token:
            raise GatewayError('Falta el token de Kushki generado en el frontend.')

        parent_user = wallet.student.parent.user

        response = requests.post(
            f'{settings.KUSHKI_API_URL}/card/v1/charges',
            json={
                'token': token,
                'amount': {
                    'subtotalIva': 0,
                    'subtotalIva0': float(amount),
                    'ice': 0,
                    'iva': 0,
                    'currency': 'USD',
                },
                'contactDetails': {
                    'documentType': 'CC',
                    'documentNumber': document_number or '',
                    'email': parent_user.email or '',
                    'firstName': parent_user.first_name,
                    'lastName': parent_user.last_name,
                    'phoneNumber': phone_number or '',
                },
            },
            headers={
                'Private-Merchant-Id': settings.KUSHKI_PRIVATE_MERCHANT_ID,
                'Content-Type': 'application/json',
            },
            timeout=10,
        )
        data = response.json()
        if response.status_code not in (200, 201) or 'ticketNumber' not in data:
            raise GatewayError(data.get('message', 'Kushki rechazó el cobro.'))
        return data['ticketNumber']
    
class PayphoneGateway(PaymentGateway):
    """Not used directly in Payphone's real flow — that goes through
    PayphonePreparer (two-step Prepare/Confirm). This class only exists so
    GatewayRouter keeps a uniform interface between gateways; calling it by
    mistake must fail explicitly, not silently."""

    gateway_code = Transaction.Gateway.PAYPHONE

    def _send_charge_request(
        self, wallet: Wallet, amount: Decimal, token: str = None,
        document_number: str = None, phone_number: str = None,
    ) -> str:
        raise NotImplementedError('Payphone real usa PayphonePreparer, no este flujo síncrono.')


class GatewayRouter:
    """Decide qué pasarela usar según el umbral de $5.00 (RF-01). El viewset
    solo conoce este router, nunca las clases concretas (Dependency Inversion)."""

    def get_gateway(self, amount: Decimal) -> PaymentGateway:
        threshold = settings.WALLET_RECHARGE_GATEWAY_THRESHOLD
        if amount < threshold:
            return PayphoneGateway()
        return KushkiGateway()


class PayphonePreparer:
    """El Botón de Pago de Payphone es un flujo de dos pasos (Prepare -> el
    usuario paga en un formulario web -> Confirm), no una llamada síncrona
    como Kushki. Por eso vive aparte del Template Method de PaymentGateway."""

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