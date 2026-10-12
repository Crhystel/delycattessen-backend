from decimal import Decimal
from unittest import mock

import pytest
import requests

from wallet.models import Transaction
from wallet.services import PayphonePreparer


def make_response(status_code=200, payload=None):
    response = mock.Mock()
    response.status_code = status_code
    response.json.return_value = payload if payload is not None else {}
    return response


@pytest.fixture
def payphone_post():
    """Replaces the HTTP call to Payphone so no test touches the network."""
    with mock.patch('wallet.services.requests.post') as post:
        yield post


@pytest.fixture
def pending_transaction(wallet):
    return Transaction.objects.create(
        wallet=wallet,
        amount=Decimal('3.00'),
        gateway=Transaction.Gateway.PAYPHONE,
        status=Transaction.Status.PENDING,
        type=Transaction.Type.RECHARGE,
    )


def test_confirm_approved_credits_balance_and_queues_email(
    wallet, pending_transaction, payphone_post, mock_celery_delay,
):
    payphone_post.return_value = make_response(200, {'transactionStatus': 'Approved'})

    result = PayphonePreparer().confirm(987, str(pending_transaction.id))

    wallet.refresh_from_db()
    pending_transaction.refresh_from_db()
    assert result.status == Transaction.Status.SUCCESS
    assert pending_transaction.status == Transaction.Status.SUCCESS
    assert wallet.balance == Decimal('13.00')
    mock_celery_delay.email.assert_called_once_with(pending_transaction.id)


@pytest.mark.parametrize('payload', [
    {'transactionStatus': 'Canceled'},
    {'transactionStatus': 'Rejected'},
    {},
])
def test_confirm_not_approved_marks_failed_and_keeps_balance(
    wallet, pending_transaction, payphone_post, mock_celery_delay, payload,
):
    payphone_post.return_value = make_response(200, payload)

    result = PayphonePreparer().confirm(987, str(pending_transaction.id))

    wallet.refresh_from_db()
    pending_transaction.refresh_from_db()
    assert result.status == Transaction.Status.FAILED
    assert pending_transaction.status == Transaction.Status.FAILED
    assert wallet.balance == Decimal('10.00')
    mock_celery_delay.email.assert_not_called()


def test_confirm_sends_expected_request_to_payphone(pending_transaction, payphone_post):
    payphone_post.return_value = make_response(200, {'transactionStatus': 'Approved'})

    PayphonePreparer().confirm(987, str(pending_transaction.id))

    args, kwargs = payphone_post.call_args
    assert args[0] == 'https://payphone.test/api/button/V2/Confirm'
    assert kwargs['json'] == {'id': 987, 'clientTxId': str(pending_transaction.id)}
    assert kwargs['headers']['Authorization'] == 'Bearer test-token'
    assert kwargs['timeout'] == 10


def test_confirm_http_error_propagates_and_leaves_transaction_pending(
    wallet, pending_transaction, payphone_post, mock_celery_delay,
):
    response = make_response(500)
    response.raise_for_status.side_effect = requests.HTTPError('server error')
    payphone_post.return_value = response

    with pytest.raises(requests.HTTPError):
        PayphonePreparer().confirm(987, str(pending_transaction.id))

    wallet.refresh_from_db()
    pending_transaction.refresh_from_db()
    assert pending_transaction.status == Transaction.Status.PENDING
    assert wallet.balance == Decimal('10.00')
    mock_celery_delay.email.assert_not_called()


def test_confirm_unknown_transaction_raises_and_skips_http(db, payphone_post):
    with pytest.raises(Transaction.DoesNotExist):
        PayphonePreparer().confirm(987, '999999')

    payphone_post.assert_not_called()


def test_confirm_already_successful_does_not_credit_twice_nor_call_payphone(
    wallet, pending_transaction, payphone_post, mock_celery_delay,
):
    payphone_post.return_value = make_response(200, {'transactionStatus': 'Approved'})
    preparer = PayphonePreparer()

    preparer.confirm(987, str(pending_transaction.id))
    preparer.confirm(987, str(pending_transaction.id))  # duplicated callback

    wallet.refresh_from_db()
    assert wallet.balance == Decimal('13.00')
    assert payphone_post.call_count == 1
    mock_celery_delay.email.assert_called_once_with(pending_transaction.id)
