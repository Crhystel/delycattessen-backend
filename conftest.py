from types import SimpleNamespace
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def fast_password_hasher(settings):
    """PBKDF2 is deliberately slow; tests only need a valid hash, not a strong one."""
    settings.PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']


@pytest.fixture(autouse=True)
def gateway_settings(settings):
    """Deterministic gateway config so tests never depend on .env values."""
    settings.WALLET_RECHARGE_GATEWAY_THRESHOLD = 5.00
    settings.KUSHKI_API_URL = 'https://kushki.test'
    settings.KUSHKI_PRIVATE_MERCHANT_ID = 'test-private-merchant'
    settings.KUSHKI_WEBHOOK_SECRET = ''
    settings.PAYPHONE_API_URL = 'https://payphone.test/api'
    settings.PAYPHONE_TOKEN = 'test-token'
    settings.PAYPHONE_STORE_ID = 'test-store'
    settings.PAYPHONE_RESPONSE_URL = 'https://app.test/payphone/response'
    settings.PAYPHONE_REDIRECT_BASE_URL = 'https://app.test/redirect'


@pytest.fixture(autouse=True)
def mock_celery_delay():
    """Prevents any test from reaching the RabbitMQ broker."""
    with mock.patch('wallet.tasks.send_recharge_confirmation_email.delay') as email_delay, \
         mock.patch('wallet.tasks.process_recharge_task.delay') as recharge_delay:
        yield SimpleNamespace(email=email_delay, recharge=recharge_delay)


@pytest.fixture(autouse=True)
def temp_media_root(settings, tmp_path):
    """Isolates MEDIA_ROOT per test into a temporary directory so media/ is untouched."""
    media_dir = tmp_path / 'media'
    media_dir.mkdir(parents=True, exist_ok=True)
    settings.MEDIA_ROOT = str(media_dir)
    return settings.MEDIA_ROOT