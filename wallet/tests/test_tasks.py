import datetime
from decimal import Decimal
from unittest import mock

import pytest

from wallet.models import Transaction, Wallet
from wallet.services import GatewayError
from wallet.tasks import process_recharge_task, send_recharge_confirmation_email

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
def recharge_transaction(wallet):
    return Transaction.objects.create(
        wallet=wallet,
        amount=Decimal('20.00'),
        gateway=Transaction.Gateway.KUSHKI,
        status=Transaction.Status.SUCCESS,
        type=Transaction.Type.RECHARGE,
        external_transaction_id='T-123',
    )


# --- process_recharge_task --------------------------------------------------

def test_process_recharge_task_success_credits_wallet(wallet, kushki_post, mock_celery_delay):
    kushki_post.return_value = make_response(200, {'ticketNumber': 'T-9'})

    process_recharge_task(wallet.id, '20.00', 'tok-1', 'CC', '1712345678', '0999999999')

    wallet.refresh_from_db()
    transaction = Transaction.objects.get()
    assert wallet.balance == Decimal('30.00')
    assert transaction.status == Transaction.Status.SUCCESS
    assert transaction.external_transaction_id == 'T-9'
    mock_celery_delay.email.assert_called_once_with(transaction.id)


def test_process_recharge_task_gateway_error_marks_failed_and_reraises(
    wallet, kushki_post, mock_celery_delay
):
    kushki_post.return_value = make_response(402, {'message': 'Card declined'})

    # Called directly (no worker), Celery's retry() re-raises the original error.
    with pytest.raises(GatewayError, match='Card declined'):
        process_recharge_task(wallet.id, '20.00', 'tok-1')

    wallet.refresh_from_db()
    assert wallet.balance == Decimal('10.00')
    assert Transaction.objects.get().status == Transaction.Status.FAILED
    mock_celery_delay.email.assert_not_called()


def test_process_recharge_task_unknown_wallet_raises_and_skips_http(kushki_post):
    with pytest.raises(Wallet.DoesNotExist):
        process_recharge_task(999999, '20.00', 'tok-1')

    kushki_post.assert_not_called()


# --- send_recharge_confirmation_email ---------------------------------------

def test_confirmation_email_builds_expected_context_in_local_time(wallet, recharge_transaction):
    # 22:30 UTC is 17:30 in America/Guayaquil (UTC-5).
    Transaction.objects.filter(pk=recharge_transaction.pk).update(
        created_at=datetime.datetime(2026, 10, 13, 22, 30, tzinfo=datetime.timezone.utc)
    )
    parent_user = wallet.student.parent.user

    with mock.patch('wallet.tasks.render_to_string', return_value='<p>Hi</p>') as render:
        send_recharge_confirmation_email(recharge_transaction.id)

    template_name, context = render.call_args.args
    assert template_name == 'emails/recharge_confirmation.html'
    assert context == {
        'parent_first_name': parent_user.first_name,
        'parent_last_name': parent_user.last_name,
        'child_name': wallet.student.user.first_name,
        'amount': Decimal('20.00'),
        'gateway': recharge_transaction.get_gateway_display(),
        'ticket_number': 'T-123',
        'reference': f'Recarga billetera #{wallet.id}',
        'date': '13/10/2026, 17:30',
    }


def test_confirmation_email_is_sent_to_parent_with_html_alternative(
    wallet, recharge_transaction, mailoutbox, settings
):
    send_recharge_confirmation_email(recharge_transaction.id)

    assert len(mailoutbox) == 1
    email = mailoutbox[0]
    assert email.to == [wallet.student.parent.user.email]
    assert email.from_email == settings.DEFAULT_FROM_EMAIL
    assert 'Confirmación de recarga' in email.subject
    assert email.body
    assert email.alternatives[0][1] == 'text/html'


def test_confirmation_email_is_skipped_when_parent_has_no_email(
    wallet, recharge_transaction, mailoutbox
):
    parent_user = wallet.student.parent.user
    parent_user.email = ''
    parent_user.save(update_fields=['email'])

    send_recharge_confirmation_email(recharge_transaction.id)

    assert mailoutbox == []


def test_confirmation_email_unknown_transaction_raises(mailoutbox):
    with pytest.raises(Transaction.DoesNotExist):
        send_recharge_confirmation_email(999999)

    assert mailoutbox == []