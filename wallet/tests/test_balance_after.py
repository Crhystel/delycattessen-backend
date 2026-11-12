import importlib
from decimal import Decimal
from unittest import mock

import pytest
import requests
from django.apps import apps
from rest_framework.test import APIClient

from wallet.models import Transaction
from wallet.services import GatewayError, KushkiGateway, PayphonePreparer, debit_wallet

pytestmark = pytest.mark.django_db

backfill_balance_after = importlib.import_module(
    'wallet.migrations.0005_transaction_balance_after'
).backfill_balance_after


def make_response(status_code=200, payload=None):
    response = mock.Mock()
    response.status_code = status_code
    response.json.return_value = payload if payload is not None else {}
    return response


@pytest.fixture
def http_post():
    with mock.patch('wallet.services.requests.post') as post:
        yield post


def make_movement(wallet, kind, amount, status=Transaction.Status.SUCCESS):
    return Transaction.objects.create(
        wallet=wallet, amount=Decimal(amount), type=kind, status=status,
    )


# --- Services store the balance right after each movement ---

def test_debit_wallet_stores_remaining_balance(wallet):
    debit_wallet(wallet, Decimal('4.00'))

    movement = Transaction.objects.get(wallet=wallet, type=Transaction.Type.CONSUMPTION)
    assert movement.balance_after == Decimal('6.00')


def test_kushki_recharge_stores_balance_after_crediting(wallet, http_post, mock_celery_delay):
    http_post.return_value = make_response(200, {'ticketNumber': 'T-1'})

    returned = KushkiGateway().process_recharge(wallet, Decimal('15.00'), token='tok')

    stored = Transaction.objects.get(pk=returned.pk)
    assert stored.status == Transaction.Status.SUCCESS
    assert stored.balance_after == Decimal('25.00')


def test_kushki_failed_recharge_has_no_balance_after(wallet, http_post, mock_celery_delay):
    http_post.return_value = make_response(402, {'message': 'Tarjeta sin fondos'})

    with pytest.raises(GatewayError):
        KushkiGateway().process_recharge(wallet, Decimal('15.00'), token='tok')

    assert Transaction.objects.get(wallet=wallet).balance_after is None


def test_payphone_approved_stores_balance_after_crediting(wallet, http_post, mock_celery_delay):
    pending = make_movement(wallet, Transaction.Type.RECHARGE, '3.00', Transaction.Status.PENDING)
    http_post.return_value = make_response(200, {'transactionStatus': 'Approved'})

    PayphonePreparer().confirm(987, str(pending.id))

    pending.refresh_from_db()
    assert pending.status == Transaction.Status.SUCCESS
    assert pending.balance_after == Decimal('13.00')


@pytest.mark.parametrize('payload', [{'transactionStatus': 'Rejected'}, {}])
def test_payphone_not_approved_has_no_balance_after(wallet, http_post, mock_celery_delay, payload):
    pending = make_movement(wallet, Transaction.Type.RECHARGE, '3.00', Transaction.Status.PENDING)
    http_post.return_value = make_response(200, payload)

    PayphonePreparer().confirm(987, str(pending.id))

    pending.refresh_from_db()
    assert pending.status == Transaction.Status.FAILED
    assert pending.balance_after is None


def test_payphone_http_error_leaves_pending_without_balance_after(wallet, http_post, mock_celery_delay):
    pending = make_movement(wallet, Transaction.Type.RECHARGE, '3.00', Transaction.Status.PENDING)
    response = make_response(500)
    response.raise_for_status.side_effect = requests.HTTPError('server error')
    http_post.return_value = response

    with pytest.raises(requests.HTTPError):
        PayphonePreparer().confirm(987, str(pending.id))

    pending.refresh_from_db()
    assert pending.status == Transaction.Status.PENDING
    assert pending.balance_after is None


# --- API ---

def test_transaction_list_exposes_balance_after(wallet):
    debit_wallet(wallet, Decimal('4.00'))
    make_movement(wallet, Transaction.Type.RECHARGE, '5.00', Transaction.Status.PENDING)
    client = APIClient()
    client.force_authenticate(wallet.student.parent.user)

    response = client.get(f'/api/wallet/{wallet.id}/transactions/')

    assert response.status_code == 200
    by_type_status = {(row['type'], row['status']): row['balance_after'] for row in response.json()}
    assert by_type_status[('consumption', 'success')] == '6.00'
    assert by_type_status[('recharge', 'pending')] is None


# --- Backfill of existing movements ---

def run_backfill():
    backfill_balance_after(apps, None)


def balances_oldest_first(wallet):
    return [m.balance_after for m in wallet.transactions.order_by('created_at', 'id')]


def test_backfill_rebuilds_mixed_sequence_from_current_balance(wallet):
    # Oldest to newest; the wallet ends at 10.00.
    make_movement(wallet, Transaction.Type.RECHARGE, '20.00')
    make_movement(wallet, Transaction.Type.CONSUMPTION, '8.00')
    make_movement(wallet, Transaction.Type.RECHARGE, '5.00', Transaction.Status.FAILED)
    make_movement(wallet, Transaction.Type.CONSUMPTION, '7.00')
    make_movement(wallet, Transaction.Type.RECHARGE, '5.00')

    run_backfill()

    # Failed movement is skipped; start balance works out to 0.
    assert balances_oldest_first(wallet) == [
        Decimal('20.00'), Decimal('12.00'), None, Decimal('5.00'), Decimal('10.00'),
    ]


def test_backfill_leaves_pending_movements_null(wallet):
    make_movement(wallet, Transaction.Type.RECHARGE, '5.00', Transaction.Status.PENDING)

    run_backfill()

    assert balances_oldest_first(wallet) == [None]


def test_backfill_leaves_older_movements_null_after_a_negative_balance(wallet):
    make_movement(wallet, Transaction.Type.CONSUMPTION, '2.00')
    make_movement(wallet, Transaction.Type.RECHARGE, '15.00')
    make_movement(wallet, Transaction.Type.CONSUMPTION, '1.00')

    run_backfill()

    # 10.00 after the last consumption, 11.00 after the recharge; the balance before the
    # recharge would be -4.00, so the oldest movement stays null.
    assert balances_oldest_first(wallet) == [None, Decimal('11.00'), Decimal('10.00')]
