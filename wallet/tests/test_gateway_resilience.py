from decimal import Decimal
from unittest import mock

import pytest
import requests
from celery.exceptions import Retry

from wallet.models import Transaction
from wallet.services import GatewayError, GatewayUnavailableError, KushkiGateway
from wallet.tasks import process_recharge_task

pytestmark = pytest.mark.django_db


def make_response(status_code, payload):
    response = mock.Mock()
    response.status_code = status_code
    response.json.return_value = payload
    return response


@pytest.fixture
def kushki_post():
    with mock.patch('wallet.services.requests.post') as post:
        yield post


@pytest.fixture
def celery_retry():
    """Replaces Task.retry so we can see whether a retry was requested."""
    with mock.patch('celery.app.task.Task.retry', side_effect=Retry()) as retry:
        yield retry


# --- KushkiGateway._send_charge_request: network failures -------------------

@pytest.mark.parametrize(
    'error',
    [requests.ConnectionError('refused'), requests.ConnectTimeout('connect timeout')],
)
def test_request_that_never_reached_kushki_raises_unavailable_error(wallet, kushki_post, error):
    kushki_post.side_effect = error

    with pytest.raises(GatewayUnavailableError, match='Kushki no está disponible'):
        KushkiGateway()._send_charge_request(wallet, Decimal('20.00'), token='tok-1')


def test_read_timeout_is_not_retryable(wallet, kushki_post):
    # Kushki may have charged the card before timing out: retrying could charge twice.
    kushki_post.side_effect = requests.ReadTimeout('read timeout')

    with pytest.raises(GatewayError) as exc_info:
        KushkiGateway()._send_charge_request(wallet, Decimal('20.00'), token='tok-1')

    assert not isinstance(exc_info.value, GatewayUnavailableError)


def test_non_json_response_raises_gateway_error(wallet, kushki_post):
    response = make_response(502, {})
    response.json.side_effect = requests.exceptions.JSONDecodeError('bad json', '', 0)
    kushki_post.return_value = response

    with pytest.raises(GatewayError) as exc_info:
        KushkiGateway()._send_charge_request(wallet, Decimal('20.00'), token='tok-1')

    assert not isinstance(exc_info.value, GatewayUnavailableError)


def test_process_recharge_with_unreachable_kushki_marks_failed_and_keeps_balance(
    wallet, kushki_post, mock_celery_delay
):
    kushki_post.side_effect = requests.ConnectionError('refused')

    with pytest.raises(GatewayUnavailableError):
        KushkiGateway().process_recharge(wallet, Decimal('20.00'), token='tok-1')

    wallet.refresh_from_db()
    assert wallet.balance == Decimal('10.00')
    assert Transaction.objects.get().status == Transaction.Status.FAILED
    mock_celery_delay.email.assert_not_called()


# --- process_recharge_task: when to retry -----------------------------------

def test_task_retries_when_kushki_is_unreachable(wallet, kushki_post, celery_retry):
    kushki_post.side_effect = requests.ConnectionError('refused')

    with pytest.raises(Retry):
        process_recharge_task(wallet.id, '20.00', 'tok-1')

    celery_retry.assert_called_once()
    assert isinstance(celery_retry.call_args.kwargs['exc'], GatewayUnavailableError)


def test_task_does_not_retry_a_definitive_rejection(wallet, kushki_post, celery_retry):
    kushki_post.return_value = make_response(402, {'message': 'Card declined'})

    with pytest.raises(GatewayError, match='Card declined'):
        process_recharge_task(wallet.id, '20.00', 'tok-1')

    celery_retry.assert_not_called()
    assert Transaction.objects.count() == 1
    assert Transaction.objects.get().status == Transaction.Status.FAILED