"""SCRUM-29, acceptance criterion 1: a tampered or forged JWT never reaches a view.

Every protected domain (payments, biometrics, POS, profile) must answer 401
to each manipulation below and must not leave any side effect behind.
"""
import base64
import json
from collections import namedtuple
from datetime import timedelta
from unittest import mock

import jwt
import pytest
from django.conf import settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from core.biometrics import BiometricValidationMixin
from pos.models import Sale
from users.models import UserBiometric
from users.services import build_qr_token
from wallet.models import Transaction

Endpoint = namedtuple('Endpoint', 'method url actor data fmt control_status')

# `actor` is the user whose token is manipulated; `control_status` is the answer
# of the same request with a genuine token (proves the route and payload are real).
ENDPOINTS = {
    'users_me': Endpoint(
        'get', lambda w: '/api/users/me/', lambda w: w.family_a.parent,
        None, None, 200),
    'wallet_transactions': Endpoint(
        'get', lambda w: f'/api/wallet/{w.family_a.wallet.id}/transactions/', lambda w: w.family_a.parent,
        None, None, 200),
    'wallet_recharge': Endpoint(
        'post', lambda w: '/api/wallet/recharge/', lambda w: w.family_a.parent,
        lambda w, photo: {'wallet_id': w.family_a.wallet.id, 'amount': '20.00', 'kushki_token': 'tok-1'},
        'json', 202),
    'biometric_register': Endpoint(
        'post', lambda w: '/api/users/biometrics/register/', lambda w: w.family_a.parent,
        lambda w, photo: {'photo': photo(), 'target_user_id': w.family_a.student_user.id},
        'multipart', 201),
    'pos_identify_qr': Endpoint(
        'post', lambda w: '/api/pos/identify/qr/', lambda w: w.operator_a,
        lambda w, photo: {'token': build_qr_token(w.family_a.student_user)[0]},
        'json', 200),
    'pos_identify_face': Endpoint(
        'post', lambda w: '/api/pos/identify/face/', lambda w: w.operator_a,
        lambda w, photo: {'photo': photo()}, 'multipart', 200),
    'pos_sale': Endpoint(
        'post', lambda w: '/api/pos/sales/', lambda w: w.operator_a,
        lambda w, photo: {
            'payment_method': 'CASH', 'items': [{'menu_item_id': w.item_a.id, 'quantity': 1}],
        },
        'json', 201),
}


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def _unb64(text):
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def _bearer(token):
    return f'Bearer {token}'


def _tampered_signature(user, world):
    head, body, signature = str(AccessToken.for_user(user)).split('.')
    # The first signature character carries all 6 bits; the last one has unused bits.
    first = 'A' if signature[0] != 'A' else 'B'
    return _bearer('.'.join((head, body, first + signature[1:])))


def _edited_payload(user, world):
    """Claims rewritten to impersonate the admin, signature kept from the genuine token."""
    head, body, signature = str(AccessToken.for_user(user)).split('.')
    claims = json.loads(_unb64(body))
    claims.update(user_id=world.admin_a.id, role='ADMIN')
    return _bearer('.'.join((head, _b64(json.dumps(claims).encode()), signature)))


def _forged_key(user, world):
    claims = dict(AccessToken.for_user(user).payload)
    return _bearer(jwt.encode(claims, 'forged-' + settings.SECRET_KEY, algorithm='HS256'))


def _alg_none(user, world):
    claims = dict(AccessToken.for_user(user).payload)
    head = _b64(json.dumps({'alg': 'none', 'typ': 'JWT'}).encode())
    return _bearer(f'{head}.{_b64(json.dumps(claims).encode())}.')


def _expired(user, world):
    token = AccessToken.for_user(user)
    token.set_exp(lifetime=-timedelta(minutes=5))
    return _bearer(token)


def _refresh_as_access(user, world):
    return _bearer(RefreshToken.for_user(user))


def _qr_as_access(user, world):
    return _bearer(build_qr_token(user)[0])


def _inactive_user(user, world):
    user.is_active = False
    user.save(update_fields=['is_active'])
    return _bearer(AccessToken.for_user(user))


def _unknown_user(user, world):
    """Genuine signature, but the user id no longer exists."""
    token = AccessToken.for_user(user)
    token['user_id'] = 2_147_483_000
    return _bearer(token)


TOKEN_CASES = {
    'tampered_signature': _tampered_signature,
    'edited_payload_keeps_signature': _edited_payload,
    'signed_with_foreign_key': _forged_key,
    'alg_none': _alg_none,
    'expired': _expired,
    'refresh_token_as_access': _refresh_as_access,
    'qr_token_as_access': _qr_as_access,
    'inactive_user': _inactive_user,
    'unknown_user': _unknown_user,
    'no_header': lambda user, world: None,
    'bearer_without_token': lambda user, world: 'Bearer',
    'bearer_garbage': lambda user, world: 'Bearer not-a-jwt',
    'bearer_three_segments_garbage': lambda user, world: 'Bearer aaa.bbb.ccc',
    'wrong_scheme': lambda user, world: 'Basic dXNlcjpwYXNz',
    'extra_segment': lambda user, world: f'Bearer {AccessToken.for_user(user)} extra',
}


def _send(client, endpoint, world, photo):
    data = endpoint.data(world, photo) if endpoint.data else None
    url = endpoint.url(world)
    if endpoint.method == 'get':
        return client.get(url)
    return client.post(url, data, format=endpoint.fmt)


def _state():
    return (
        Transaction.objects.count(),
        Sale.objects.count(),
        sorted((b.user_id, bytes(b.encrypted_embedding)) for b in UserBiometric.objects.all()),
    )


@pytest.mark.parametrize('token_case', TOKEN_CASES)
@pytest.mark.parametrize('endpoint_name', ENDPOINTS)
def test_tampered_token_is_rejected_with_401(
    endpoint_name, token_case, security_world, photo, store_face, mock_celery_delay,
):
    """SEC-JWT-01: every manipulated token gets 401 on every domain, with no side effect."""
    endpoint = ENDPOINTS[endpoint_name]
    store_face(security_world.family_a.student_user, seed=1)
    header = TOKEN_CASES[token_case](endpoint.actor(security_world), security_world)
    client = APIClient()
    if header is not None:
        client.credentials(HTTP_AUTHORIZATION=header)
    before = _state()

    response = _send(client, endpoint, security_world, photo)

    assert response.status_code == 401, response.data
    assert 'detail' in response.data
    assert _state() == before
    mock_celery_delay.recharge.assert_not_called()


@pytest.mark.parametrize('endpoint_name', ENDPOINTS)
def test_genuine_token_is_accepted(
    endpoint_name, security_world, client_for, photo, store_face,
):
    """SEC-JWT-02: control case; the same requests succeed with an untouched token."""
    endpoint = ENDPOINTS[endpoint_name]
    vector = store_face(security_world.family_a.student_user, seed=1)
    client = client_for(endpoint.actor(security_world))

    with mock.patch.object(BiometricValidationMixin, 'extract_face_embedding', return_value=vector):
        response = _send(client, endpoint, security_world, photo)

    assert response.status_code == endpoint.control_status, response.data


REFRESH_URLS = ('/api/token/refresh/', '/api/users/login/refresh/')


def _forged_refresh(user):
    claims = dict(RefreshToken.for_user(user).payload)
    return jwt.encode(claims, 'forged-' + settings.SECRET_KEY, algorithm='HS256')


def _tampered_refresh(user):
    head, body, signature = str(RefreshToken.for_user(user)).split('.')
    first = 'A' if signature[0] != 'A' else 'B'
    return '.'.join((head, body, first + signature[1:]))


@pytest.mark.parametrize('url', REFRESH_URLS)
@pytest.mark.parametrize('build_token', [
    _tampered_refresh,
    _forged_refresh,
    lambda user: str(AccessToken.for_user(user)),
    lambda user: 'not-a-jwt',
], ids=['tampered_signature', 'signed_with_foreign_key', 'access_token_as_refresh', 'garbage'])
def test_tampered_refresh_token_is_rejected(url, build_token, security_world):
    """SEC-JWT-03: a manipulated refresh token cannot mint a new access token."""
    token = build_token(security_world.family_a.parent)

    response = APIClient().post(url, {'refresh': token}, format='json')

    assert response.status_code == 401, response.data
    assert 'access' not in response.data
