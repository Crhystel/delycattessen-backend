from decimal import Decimal

import pytest

from wallet.models import Transaction
from wallet.services import GatewayRouter, KushkiGateway, PayphoneGateway


@pytest.mark.parametrize('amount, expected_gateway', [
    (Decimal('0.01'), PayphoneGateway),
    (Decimal('4.99'), PayphoneGateway),
    (Decimal('5.00'), KushkiGateway),   # boundary: threshold itself goes to Kushki
    (Decimal('5.01'), KushkiGateway),
    (Decimal('100.00'), KushkiGateway),
])
def test_get_gateway_routes_by_threshold(amount, expected_gateway):
    gateway = GatewayRouter().get_gateway(amount)

    assert isinstance(gateway, expected_gateway)


def test_get_gateway_returns_matching_gateway_code():
    router = GatewayRouter()

    assert router.get_gateway(Decimal('1.00')).gateway_code == Transaction.Gateway.PAYPHONE
    assert router.get_gateway(Decimal('50.00')).gateway_code == Transaction.Gateway.KUSHKI


def test_get_gateway_uses_configured_threshold(settings):
    settings.WALLET_RECHARGE_GATEWAY_THRESHOLD = 10.00
    router = GatewayRouter()

    assert isinstance(router.get_gateway(Decimal('9.99')), PayphoneGateway)
    assert isinstance(router.get_gateway(Decimal('10.00')), KushkiGateway)
