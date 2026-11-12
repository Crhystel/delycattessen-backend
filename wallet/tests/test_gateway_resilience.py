from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
import requests
from celery.exceptions import Retry
from django.utils import timezone

from wallet.models import Transaction
from wallet.services import (
    GatewayError, GatewayUnavailableError, KushkiGateway, expire_unclaimed_recharges,
)
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

def queued_recharge(wallet, amount='20.00'):
    """The pending record RechargeService creates before queuing the task."""
    return KushkiGateway()._create_pending_transaction(wallet, Decimal(amount))


def test_task_retries_when_kushki_is_unreachable(wallet, kushki_post, celery_retry):
    kushki_post.side_effect = requests.ConnectionError('refused')
    pending = queued_recharge(wallet)

    with pytest.raises(Retry):
        process_recharge_task(pending.id, 'tok-1')

    celery_retry.assert_called_once()
    assert isinstance(celery_retry.call_args.kwargs['exc'], GatewayUnavailableError)
    # The same record waits for the retry, given back to the queue.
    pending.refresh_from_db()
    assert (pending.status, pending.claimed_at) == (Transaction.Status.PENDING, None)
    assert Transaction.objects.count() == 1


def test_task_does_not_retry_a_definitive_rejection(wallet, kushki_post, celery_retry):
    kushki_post.return_value = make_response(402, {'message': 'Card declined'})

    pending = queued_recharge(wallet)

    with pytest.raises(GatewayError, match='Card declined'):
        process_recharge_task(pending.id, 'tok-1')

    celery_retry.assert_not_called()
    assert Transaction.objects.count() == 1
    assert Transaction.objects.get().status == Transaction.Status.FAILED

# --- worker down: recharges that wait too long in the queue -----------------

def test_task_charges_the_queued_record_and_credits_once(wallet, kushki_post, mock_celery_delay):
    kushki_post.return_value = make_response(201, {'ticketNumber': 'T-1'})
    pending = queued_recharge(wallet)

    process_recharge_task(pending.id, 'tok-1')
    process_recharge_task(pending.id, 'tok-1')  # redelivered message: already taken

    pending.refresh_from_db()
    wallet.refresh_from_db()
    assert pending.status == Transaction.Status.SUCCESS
    assert wallet.balance == Decimal('30.00')
    assert kushki_post.call_count == 1


def test_recharge_expired_in_the_queue_is_failed_and_never_charged(wallet, kushki_post, settings):
    settings.RECHARGE_QUEUE_TIMEOUT_SECONDS = 60
    pending = queued_recharge(wallet)
    Transaction.objects.filter(pk=pending.pk).update(created_at=timezone.now() - timedelta(seconds=61))

    process_recharge_task(pending.id, 'tok-1')  # the worker came back too late

    pending.refresh_from_db()
    wallet.refresh_from_db()
    assert pending.status == Transaction.Status.FAILED
    assert wallet.balance == Decimal('10.00')
    kushki_post.assert_not_called()


def test_expiry_only_touches_unclaimed_kushki_recharges(wallet, settings):
    settings.RECHARGE_QUEUE_TIMEOUT_SECONDS = 60
    old = timezone.now() - timedelta(seconds=61)
    waiting = queued_recharge(wallet)
    taken = queued_recharge(wallet)
    recent = queued_recharge(wallet)
    Transaction.objects.filter(pk__in=[waiting.pk, taken.pk]).update(created_at=old)
    Transaction.objects.filter(pk=taken.pk).update(claimed_at=old)

    assert expire_unclaimed_recharges() == 1
    statuses = dict(Transaction.objects.values_list('pk', 'status'))
    assert statuses == {
        waiting.pk: Transaction.Status.FAILED,
        taken.pk: Transaction.Status.PENDING,
        recent.pk: Transaction.Status.PENDING,
    }


def test_last_retry_with_kushki_unreachable_fails_the_recharge(wallet, kushki_post):
    kushki_post.side_effect = requests.ConnectionError('refused')
    pending = queued_recharge(wallet)

    with mock.patch.object(process_recharge_task, 'max_retries', 0):
        process_recharge_task(pending.id, 'tok-1')

    pending.refresh_from_db()
    assert pending.status == Transaction.Status.FAILED
