from decimal import Decimal
from unittest import mock

import pytest

from wallet.models import Transaction
from wallet.services import GatewayError, KushkiGateway


def make_response(status_code=200, payload=None):
    response = mock.Mock()
    response.status_code = status_code
    response.json.return_value = payload if payload is not None else {}
    return response


@pytest.fixture
def kushki_post():
    """Replaces the HTTP call to Kushki so no test touches the network."""
    with mock.patch('wallet.services.requests.post') as post:
        yield post


# --- KushkiGateway._send_charge_request: one test per branch ---

def test_send_charge_request_without_token_raises_and_skips_http(wallet, kushki_post):
    with pytest.raises(GatewayError, match='Falta el token'):
        KushkiGateway()._send_charge_request(wallet, Decimal('10.00'))

    kushki_post.assert_not_called()


@pytest.mark.parametrize('status_code', [400, 402, 500])
def test_send_charge_request_rejected_status_raises_gateway_message(wallet, kushki_post, status_code):
    kushki_post.return_value = make_response(status_code, {'message': 'Tarjeta sin fondos'})

    with pytest.raises(GatewayError, match='Tarjeta sin fondos'):
        KushkiGateway()._send_charge_request(wallet, Decimal('10.00'), token='tok_123')


def test_send_charge_request_without_ticket_number_raises_default_message(wallet, kushki_post):
    kushki_post.return_value = make_response(200, {})

    with pytest.raises(GatewayError, match='Kushki rech'):
        KushkiGateway()._send_charge_request(wallet, Decimal('10.00'), token='tok_123')


@pytest.mark.parametrize('status_code', [200, 201])
def test_send_charge_request_success_returns_ticket_number(wallet, kushki_post, status_code):
    kushki_post.return_value = make_response(status_code, {'ticketNumber': 'T-123'})

    ticket = KushkiGateway()._send_charge_request(wallet, Decimal('10.00'), token='tok_123')

    assert ticket == 'T-123'


def test_send_charge_request_builds_expected_payload(wallet, kushki_post):
    kushki_post.return_value = make_response(200, {'ticketNumber': 'T-123'})

    KushkiGateway()._send_charge_request(
        wallet, Decimal('12.50'), token='tok_123',
        document_number='1712345678', phone_number='0991234567',
    )

    args, kwargs = kushki_post.call_args
    assert args[0] == 'https://kushki.test/card/v1/charges'
    assert kwargs['headers']['Private-Merchant-Id'] == 'test-private-merchant'
    assert kwargs['timeout'] == 10
    body = kwargs['json']
    assert body['token'] == 'tok_123'
    assert body['amount']['subtotalIva0'] == 12.5
    assert body['amount']['currency'] == 'USD'
    assert body['contactDetails'] == {
        'documentType': 'CC',
        'documentNumber': '1712345678',
        'email': 'parent1@test.com',
        'firstName': 'Ana',
        'lastName': 'Lopez',
        'phoneNumber': '0991234567',
    }


# --- PaymentGateway.process_recharge (Template Method flow) via Kushki ---

def test_process_recharge_success_credits_balance_and_queues_email(wallet, kushki_post, mock_celery_delay):
    kushki_post.return_value = make_response(200, {'ticketNumber': 'T-123'})

    transaction = KushkiGateway().process_recharge(wallet, Decimal('15.00'), token='tok_123')

    wallet.refresh_from_db()
    assert wallet.balance == Decimal('25.00')
    assert transaction.status == Transaction.Status.SUCCESS
    assert transaction.type == Transaction.Type.RECHARGE
    assert transaction.gateway == Transaction.Gateway.KUSHKI
    assert transaction.external_transaction_id == 'T-123'
    assert transaction.amount == Decimal('15.00')
    mock_celery_delay.email.assert_called_once_with(transaction.id)


def test_process_recharge_gateway_failure_marks_failed_and_keeps_balance(wallet, kushki_post, mock_celery_delay):
    kushki_post.return_value = make_response(402, {'message': 'Tarjeta sin fondos'})

    with pytest.raises(GatewayError):
        KushkiGateway().process_recharge(wallet, Decimal('15.00'), token='tok_123')

    wallet.refresh_from_db()
    transaction = Transaction.objects.get(wallet=wallet)
    assert transaction.status == Transaction.Status.FAILED
    assert wallet.balance == Decimal('10.00')
    mock_celery_delay.email.assert_not_called()


@pytest.mark.parametrize('amount', [Decimal('0'), Decimal('-1.00')])
def test_process_recharge_invalid_amount_creates_nothing(wallet, kushki_post, amount):
    with pytest.raises(ValueError):
        KushkiGateway().process_recharge(wallet, amount, token='tok_123')

    assert Transaction.objects.count() == 0
    kushki_post.assert_not_called()
