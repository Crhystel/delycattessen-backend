from decimal import Decimal
from unittest import mock
from urllib.parse import quote

import pytest
import requests
from wallet.models import Transaction
from wallet.services import GatewayError, PayphonePreparer

pytestmark = pytest.mark.django_db


def make_response(status_code, payload=None, content=b'{}'):
    response = mock.Mock()
    response.status_code = status_code
    response.content = content
    response.json.return_value = payload if payload is not None else {}
    return response


@pytest.fixture
def prepare_post():
    with mock.patch('wallet.services.requests.post') as post:
        post.return_value = make_response(
            200, {'payWithCard': 'https://payphone.test/pay?x=1&y=2', 'paymentId': 777}
        )
        yield post


def test_prepare_success_returns_redirect_url_and_keeps_transaction_pending(
    wallet, settings, prepare_post
):
    result = PayphonePreparer().prepare(wallet, Decimal('3.00'))

    transaction = Transaction.objects.get(pk=result['transaction_id'])
    expected_url = (
        f"{settings.PAYPHONE_REDIRECT_BASE_URL}"
        f"?target={quote('https://payphone.test/pay?x=1&y=2', safe='')}"
    )
    assert result['payment_url'] == expected_url
    assert transaction.status == Transaction.Status.PENDING
    assert transaction.gateway == Transaction.Gateway.PAYPHONE
    assert transaction.type == Transaction.Type.RECHARGE
    assert transaction.amount == Decimal('3.00')
    assert transaction.external_transaction_id == '777'
    wallet.refresh_from_db()
    assert wallet.balance == Decimal('10.00')


def test_prepare_sends_expected_request_to_payphone(wallet, settings, prepare_post):
    result = PayphonePreparer().prepare(wallet, Decimal('3.00'))

    prepare_post.assert_called_once()
    args, kwargs = prepare_post.call_args
    assert args == ('https://payphone.test/api/button/Prepare',)
    assert kwargs['json'] == {
        'amount': 300,
        'amountWithoutTax': 300,
        'clientTransactionId': str(result['transaction_id']),
        'currency': 'USD',
        'storeId': 'test-store',
        'reference': f'Recarga billetera #{wallet.id}',
        'responseUrl': settings.PAYPHONE_RESPONSE_URL,
    }
    assert kwargs['headers']['Authorization'] == 'Bearer test-token'
    assert kwargs['timeout'] == 10


@pytest.mark.parametrize(
    'amount, expected_cents',
    [(Decimal('0.01'), 1), (Decimal('3.50'), 350), (Decimal('4.99'), 499)],
)
def test_prepare_converts_amount_to_cents_without_rounding_errors(
    wallet, prepare_post, amount, expected_cents
):
    PayphonePreparer().prepare(wallet, amount)

    assert prepare_post.call_args.kwargs['json']['amount'] == expected_cents
    assert prepare_post.call_args.kwargs['json']['amountWithoutTax'] == expected_cents


@pytest.mark.parametrize('status_code', [400, 500])
def test_prepare_rejected_marks_transaction_failed_and_raises_gateway_message(
    wallet, prepare_post, status_code
):
    prepare_post.return_value = make_response(status_code, {'message': 'Store disabled'})

    with pytest.raises(GatewayError, match='Store disabled'):
        PayphonePreparer().prepare(wallet, Decimal('3.00'))

    transaction = Transaction.objects.get()
    assert transaction.status == Transaction.Status.FAILED
    assert transaction.external_transaction_id in (None, '')


def test_prepare_rejected_without_body_uses_default_message(wallet, prepare_post):
    prepare_post.return_value = make_response(502, content=b'')

    with pytest.raises(GatewayError, match='Payphone rechaz'):
        PayphonePreparer().prepare(wallet, Decimal('3.00'))

    assert Transaction.objects.get().status == Transaction.Status.FAILED
    prepare_post.return_value.json.assert_not_called()

@pytest.mark.parametrize(
    'error', [requests.ConnectionError('down'), requests.Timeout('slow')]
)
def test_prepare_network_failure_marks_transaction_failed_and_raises_gateway_error(
    wallet, prepare_post, error
):
    prepare_post.side_effect = error

    with pytest.raises(GatewayError, match='Payphone no está disponible'):
        PayphonePreparer().prepare(wallet, Decimal('3.00'))

    assert Transaction.objects.get().status == Transaction.Status.FAILED