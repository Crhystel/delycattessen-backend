from decimal import Decimal
from types import SimpleNamespace
from unittest import mock
import requests
import pytest
from rest_framework.test import APIClient

from wallet.models import Transaction
from wallet.services import GatewayError

pytestmark = pytest.mark.django_db

RECHARGE_URL = '/api/wallet/recharge/'
CALLBACK_URL = '/api/wallet/payphone/callback/'
REDIRECT_URL = '/api/wallet/payphone/redirect/'


def transactions_url(wallet_id):
    return f'/api/wallet/{wallet_id}/transactions/'


@pytest.fixture
def anonymous_client():
    return APIClient()


@pytest.fixture
def parent_client(wallet):
    client = APIClient()
    client.force_authenticate(wallet.student.parent.user)
    return client


@pytest.fixture
def student_client(wallet):
    # Authenticated but not the wallet's parent.
    client = APIClient()
    client.force_authenticate(wallet.student.user)
    return client


def make_recharge(wallet, **overrides):
    data = {
        'wallet': wallet,
        'amount': Decimal('5.00'),
        'gateway': Transaction.Gateway.PAYPHONE,
        'status': Transaction.Status.SUCCESS,
        'type': Transaction.Type.RECHARGE,
    }
    data.update(overrides)
    return Transaction.objects.create(**data)


# --- WalletRechargeView -----------------------------------------------------

@pytest.mark.parametrize('amount', ['0', '-5.00', 'abc'])
def test_recharge_rejects_invalid_amount(parent_client, wallet, mock_celery_delay, amount):
    response = parent_client.post(
        RECHARGE_URL, {'wallet_id': wallet.id, 'amount': amount}, format='json'
    )

    assert response.status_code == 400
    mock_celery_delay.recharge.assert_not_called()


def test_recharge_unknown_wallet_returns_400(parent_client):
    response = parent_client.post(
        RECHARGE_URL, {'wallet_id': 999999, 'amount': '10.00'}, format='json'
    )

    assert response.status_code == 400
    assert 'wallet_id' in response.json()


def test_recharge_below_threshold_returns_payment_url(parent_client, wallet):
    prepared = {'transaction_id': 55, 'payment_url': 'https://pay.test/checkout'}
    with mock.patch('wallet.views.PayphonePreparer.prepare', return_value=prepared) as prepare:
        response = parent_client.post(
            RECHARGE_URL, {'wallet_id': wallet.id, 'amount': '3.00'}, format='json'
        )

    assert response.status_code == 200
    assert response.json() == {'transaction_id': 55, 'payment_url': 'https://pay.test/checkout'}
    prepare.assert_called_once_with(wallet, Decimal('3.00'))


def test_recharge_payphone_gateway_error_returns_400(parent_client, wallet):
    with mock.patch(
        'wallet.views.PayphonePreparer.prepare', side_effect=GatewayError('Payphone down')
    ):
        response = parent_client.post(
            RECHARGE_URL, {'wallet_id': wallet.id, 'amount': '3.00'}, format='json'
        )

    assert response.status_code == 400
    assert response.json() == {'detail': 'Payphone down'}


def test_recharge_from_threshold_queues_kushki_task(parent_client, wallet, mock_celery_delay):
    payload = {
        'wallet_id': wallet.id,
        'amount': '20.00',
        'kushki_token': 'tok-1',
        'document_type': 'CC',
        'document_number': '1712345678',
        'phone_number': '0999999999',
    }

    response = parent_client.post(RECHARGE_URL, payload, format='json')

    assert response.status_code == 202
    mock_celery_delay.recharge.assert_called_once_with(
        wallet.id, '20.00', 'tok-1', 'CC', '1712345678', '0999999999'
    )


@pytest.mark.parametrize('token_payload', [{}, {'kushki_token': ''}])
def test_recharge_kushki_range_requires_token(
    parent_client, wallet, mock_celery_delay, token_payload
):
    payload = {'wallet_id': wallet.id, 'amount': '20.00', **token_payload}

    response = parent_client.post(RECHARGE_URL, payload, format='json')

    assert response.status_code == 400
    assert response.json() == {'detail': 'Falta el token de Kushki.'}
    mock_celery_delay.recharge.assert_not_called()


@pytest.mark.parametrize('amount, expected_status', [('4.99', 200), ('5.00', 202)])
def test_recharge_routing_boundary(parent_client, wallet, mock_celery_delay, amount, expected_status):
    prepared = {'transaction_id': 1, 'payment_url': 'https://pay.test/checkout'}
    with mock.patch('wallet.views.PayphonePreparer.prepare', return_value=prepared):
        response = parent_client.post(
            RECHARGE_URL,
            {'wallet_id': wallet.id, 'amount': amount, 'kushki_token': 'tok-1'},
            format='json',
        )

    assert response.status_code == expected_status


def test_recharge_by_non_owner_is_forbidden(student_client, wallet, mock_celery_delay):
    response = student_client.post(
        RECHARGE_URL,
        {'wallet_id': wallet.id, 'amount': '20.00', 'kushki_token': 'tok-1'},
        format='json',
    )

    assert response.status_code == 403
    mock_celery_delay.recharge.assert_not_called()


def test_recharge_requires_authentication(anonymous_client, wallet):
    response = anonymous_client.post(
        RECHARGE_URL, {'wallet_id': wallet.id, 'amount': '20.00'}, format='json'
    )

    assert response.status_code in (401, 403)


# --- PayphoneCallbackView: entradas inválidas y errores externos ------------

def test_callback_non_numeric_payphone_id_returns_400(anonymous_client):
    with mock.patch('wallet.views.PayphonePreparer.confirm') as confirm:
        response = anonymous_client.get(CALLBACK_URL, {'id': 'abc', 'clientTransactionId': '5'})

    assert response.status_code == 400
    confirm.assert_not_called()


def test_callback_non_numeric_client_transaction_id_returns_400(anonymous_client):
    with mock.patch('wallet.views.PayphonePreparer.confirm') as confirm:
        response = anonymous_client.get(CALLBACK_URL, {'id': '987', 'clientTransactionId': 'abc'})

    assert response.status_code == 400
    confirm.assert_not_called()


def test_callback_unknown_transaction_returns_404(anonymous_client):
    with mock.patch('wallet.services.requests.post') as post:
        response = anonymous_client.get(
            CALLBACK_URL, {'id': '987', 'clientTransactionId': '999999'}
        )

    assert response.status_code == 404
    post.assert_not_called()


def test_callback_payphone_failure_returns_502(anonymous_client):
    with mock.patch(
        'wallet.views.PayphonePreparer.confirm', side_effect=requests.HTTPError('boom')
    ):
        response = anonymous_client.get(CALLBACK_URL, {'id': '987', 'clientTransactionId': '5'})

    assert response.status_code == 502


# --- PayphoneRedirectView: solo hosts permitidos ----------------------------

def test_redirect_renders_page_with_referrer_policy(anonymous_client):
    target = 'https://payphone.test/checkout'

    response = anonymous_client.get(REDIRECT_URL, {'target': target})

    assert response.status_code == 200
    assert response['Referrer-Policy'] == 'strict-origin-when-cross-origin'
    assert target in response.content.decode()


@pytest.mark.parametrize(
    'target',
    [
        'https://evil.test/phishing',
        'javascript:alert(1)',
        'http://payphone.test/checkout',  # not https
    ],
)
def test_redirect_rejects_disallowed_targets(anonymous_client, target):
    response = anonymous_client.get(REDIRECT_URL, {'target': target})

    assert response.status_code == 400


def test_redirect_escapes_script_breakout_in_target(anonymous_client):
    target = "https://payphone.test/x</script><script>alert(1)</script>"

    response = anonymous_client.get(REDIRECT_URL, {'target': target})

    assert '</script><script>' not in response.content.decode()

# --- PayphoneCallbackView: parámetros y delegación --------------------------

@pytest.mark.parametrize('params', [{}, {'id': '987'}, {'clientTransactionId': '5'}])
def test_callback_missing_params_returns_400(anonymous_client, params):
    with mock.patch('wallet.views.PayphonePreparer.confirm') as confirm:
        response = anonymous_client.get(CALLBACK_URL, params)

    assert response.status_code == 400
    confirm.assert_not_called()


def test_callback_delegates_to_confirm_and_returns_status(anonymous_client):
    confirmed = SimpleNamespace(status=Transaction.Status.SUCCESS)
    with mock.patch('wallet.views.PayphonePreparer.confirm', return_value=confirmed) as confirm:
        response = anonymous_client.get(CALLBACK_URL, {'id': '987', 'clientTransactionId': '5'})

    assert response.status_code == 200
    assert response.json() == {'status': 'success'}
    confirm.assert_called_once_with(987, '5')


# --- WalletTransactionListView ----------------------------------------------

def test_transaction_list_returns_wallet_recharges(parent_client, wallet):
    make_recharge(wallet, amount=Decimal('5.00'))
    make_recharge(wallet, amount=Decimal('20.00'), gateway=Transaction.Gateway.KUSHKI)

    response = parent_client.get(transactions_url(wallet.id))

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert {item['display_name'] for item in body} == {'Recarga de saldo'}
    assert all(item['items'] == [] for item in body)


def test_transaction_list_with_preorder_dish(parent_client, wallet):
    from catalog.models import MenuDish, MonthlyMenu, DailyMenu
    from pos.models import PreOrder, PreOrderItem
    import datetime

    # Create dish and preorder
    dish = MenuDish.objects.create(
        name="Ensalada Cesar",
        description="Ensalada fresca",
        price=Decimal("3.50"),
        institution=wallet.student.institution,
    )
    monthly_menu = MonthlyMenu.objects.create(
        institution=wallet.student.institution,
        month=10,
        year=2026,
    )
    daily_menu = DailyMenu.objects.create(
        monthly_menu=monthly_menu,
        date=datetime.date(2026, 10, 20),
    )
    preorder = PreOrder.objects.create(
        student=wallet.student,
        daily_menu=daily_menu,
        total_amount=Decimal("3.50"),
        status=PreOrder.Status.PENDING,
    )
    PreOrderItem.objects.create(
        pre_order=preorder,
        dish=dish,
        quantity=1,
        price_at_purchase=Decimal("3.50"),
    )
    Transaction.objects.create(
        wallet=wallet,
        amount=Decimal("3.50"),
        type=Transaction.Type.CONSUMPTION,
        status=Transaction.Status.SUCCESS,
        pre_order=preorder,
    )

    response = parent_client.get(transactions_url(wallet.id))

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]['display_name'] == 'Ensalada Cesar'
    assert len(body[0]['items']) == 1
    assert body[0]['items'][0]['menu_item_name'] == 'Ensalada Cesar'
    assert body[0]['items'][0]['quantity'] == 1
    assert body[0]['items'][0]['price_at_purchase'] == '3.50'



def test_transaction_list_is_limited_to_ten(parent_client, wallet):
    for _ in range(12):
        make_recharge(wallet)

    response = parent_client.get(transactions_url(wallet.id))

    assert response.status_code == 200
    assert len(response.json()) == 10


def test_transaction_list_by_non_owner_is_forbidden(student_client, wallet):
    response = student_client.get(transactions_url(wallet.id))

    assert response.status_code == 403


def test_transaction_list_requires_authentication(anonymous_client, wallet):
    response = anonymous_client.get(transactions_url(wallet.id))

    assert response.status_code in (401, 403)


def test_transaction_list_unknown_wallet_returns_404(parent_client):
    response = parent_client.get(transactions_url(999999))

    assert response.status_code == 404


# --- PayphoneRedirectView ---------------------------------------------------

def test_redirect_without_target_returns_400(anonymous_client):
    response = anonymous_client.get(REDIRECT_URL)

    assert response.status_code == 400

# --- RechargeGatewayView ----------------------------------------------------

GATEWAY_URL = '/api/wallet/recharge/gateway/'


@pytest.mark.parametrize('amount, expected', [
    ('1.00', 'payphone'),
    ('4.99', 'payphone'),
    ('5.00', 'kushki'),   # the threshold itself goes to Kushki
    ('50.00', 'kushki'),
])
def test_gateway_endpoint_routes_by_amount(parent_client, amount, expected):
    response = parent_client.get(GATEWAY_URL, {'amount': amount})

    assert response.status_code == 200
    assert response.data == {'gateway': expected}


def test_gateway_endpoint_follows_configured_threshold(parent_client, settings):
    settings.WALLET_RECHARGE_GATEWAY_THRESHOLD = 20.00

    assert parent_client.get(GATEWAY_URL, {'amount': '10.00'}).data == {'gateway': 'payphone'}
    assert parent_client.get(GATEWAY_URL, {'amount': '20.00'}).data == {'gateway': 'kushki'}


@pytest.mark.parametrize('amount', [None, 'abc', '0', '-3', '0.50'])
def test_gateway_endpoint_rejects_invalid_or_below_minimum(parent_client, amount):
    params = {} if amount is None else {'amount': amount}

    assert parent_client.get(GATEWAY_URL, params).status_code == 400


def test_gateway_endpoint_minimum_is_configurable(parent_client, settings):
    settings.WALLET_RECHARGE_MIN_AMOUNT = 0.25

    assert parent_client.get(GATEWAY_URL, {'amount': '0.50'}).status_code == 200


def test_gateway_endpoint_requires_a_parent(anonymous_client, student_client):
    assert anonymous_client.get(GATEWAY_URL, {'amount': '5.00'}).status_code in (401, 403)
    assert student_client.get(GATEWAY_URL, {'amount': '5.00'}).status_code == 403


def test_recharge_rejects_amount_below_minimum(parent_client, wallet, mock_celery_delay):
    response = parent_client.post(
        RECHARGE_URL, {'wallet_id': wallet.id, 'amount': '0.50'}, format='json'
    )

    assert response.status_code == 400
    assert 'amount' in response.data
