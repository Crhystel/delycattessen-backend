from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from wallet.models import Transaction

pytestmark = pytest.mark.django_db

URL = '/api/wallet/kushki/webhook/'


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def kushki_transaction(wallet):
    return Transaction.objects.create(
        wallet=wallet,
        amount=Decimal('20.00'),
        gateway=Transaction.Gateway.KUSHKI,
        status=Transaction.Status.PENDING,
        type=Transaction.Type.RECHARGE,
        external_transaction_id='T-123',
    )


def test_missing_ticket_number_returns_400(api_client, mock_celery_delay):
    response = api_client.post(URL, {'transactionStatus': 'APPROVED'}, format='json')

    assert response.status_code == 400
    assert response.json() == {'detail': 'Missing ticketNumber.'}
    mock_celery_delay.email.assert_not_called()


def test_unknown_ticket_is_acknowledged_without_changes(
    api_client, kushki_transaction, mock_celery_delay
):
    response = api_client.post(
        URL, {'ticketNumber': 'UNKNOWN', 'transactionStatus': 'APPROVED'}, format='json'
    )

    assert response.status_code == 200
    assert response.json() == {'received': True}
    kushki_transaction.refresh_from_db()
    assert kushki_transaction.status == Transaction.Status.PENDING
    mock_celery_delay.email.assert_not_called()


def test_approved_marks_transaction_success_and_queues_email(
    api_client, kushki_transaction, mock_celery_delay
):
    response = api_client.post(
        URL, {'ticketNumber': 'T-123', 'transactionStatus': 'APPROVED'}, format='json'
    )

    assert response.status_code == 200
    assert response.json() == {'received': True}
    kushki_transaction.refresh_from_db()
    assert kushki_transaction.status == Transaction.Status.SUCCESS
    mock_celery_delay.email.assert_called_once_with(kushki_transaction.id)


def test_repeated_approved_webhook_queues_email_only_once(
    api_client, kushki_transaction, mock_celery_delay
):
    payload = {'ticketNumber': 'T-123', 'transactionStatus': 'APPROVED'}

    api_client.post(URL, payload, format='json')
    response = api_client.post(URL, payload, format='json')

    assert response.status_code == 200
    mock_celery_delay.email.assert_called_once()


@pytest.mark.parametrize('webhook_status', ['DECLINED', 'INITIALIZED', '', None])
def test_non_approved_status_leaves_transaction_unchanged(
    api_client, kushki_transaction, mock_celery_delay, webhook_status
):
    payload = {'ticketNumber': 'T-123', 'transactionStatus': webhook_status}

    response = api_client.post(URL, payload, format='json')

    assert response.status_code == 200
    kushki_transaction.refresh_from_db()
    assert kushki_transaction.status == Transaction.Status.PENDING
    mock_celery_delay.email.assert_not_called()


def test_webhook_does_not_require_authentication(api_client, kushki_transaction):
    # AllowAny: Kushki calls this endpoint without credentials.
    response = api_client.post(URL, {'ticketNumber': 'T-123'}, format='json')

    assert response.status_code == 200