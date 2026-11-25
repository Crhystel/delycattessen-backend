"""SCRUM-29, acceptance criterion 2: integration tests of isolation.

Covers the three borders of the system: institution (sede) vs institution,
biometric domain vs payments domain, and parent vs parent. Each denied
request must also leave the stored data untouched.
"""
from operator import attrgetter
from unittest import mock

import pytest
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from core.biometrics import BiometricValidationMixin
from pos.models import Sale
from users.models import UserBiometric
from users.services import build_qr_token
from wallet.models import Transaction, Wallet

BIOMETRIC_KEYS = {'embedding', 'encrypted_embedding', 'nonce', 'tag', 'vector'}
PAYMENT_KEYS = {'balance', 'wallet', 'wallet_id', 'transactions', 'amount'}


def _wallet_list(w, client, photo):
    return client.get(f'/api/wallet/{w.family_a.wallet.id}/transactions/')


def _wallet_recharge(w, client, photo):
    return client.post(
        '/api/wallet/recharge/',
        {'wallet_id': w.family_a.wallet.id, 'amount': '20.00', 'kushki_token': 'tok-1'},
        format='json',
    )


def _register_biometric(w, client, photo):
    return client.post(
        '/api/users/biometrics/register/',
        {'photo': photo(), 'target_user_id': w.family_a.student_user.id},
        format='multipart',
    )


def _identify_qr(w, client, photo):
    return client.post(
        '/api/pos/identify/qr/', {'token': build_qr_token(w.family_a.student_user)[0]}, format='json',
    )


def _identify_face(w, client, photo):
    return client.post('/api/pos/identify/face/', {'photo': photo()}, format='multipart')


ACTIONS = {
    'wallet_list': _wallet_list,
    'wallet_recharge': _wallet_recharge,
    'biometric_register': _register_biometric,
    'pos_identify_qr': _identify_qr,
    'pos_identify_face': _identify_face,
}

DENIED = (403, 403, 403, 403, 403)

# Everything is aimed at the student of family A (sede A). Expected status per
# action, in ACTIONS order. Only the owner parent touches wallet and biometrics;
# only POS staff of the same sede (or an admin) identifies the student.
ROLE_MATRIX = {
    'family_a.parent': (200, 202, 201, 403, 403),
    'family_a2.parent': DENIED,
    'family_b.parent': DENIED,
    'family_a.student_user': DENIED,
    'teacher_a': DENIED,
    'operator_a': (403, 403, 403, 200, 200),
    'operator_b': (403, 403, 403, 403, 404),
    'admin_a': (403, 403, 403, 200, 200),
}
MATRIX_CASES = [
    pytest.param(actor, action, expected, id=f'{actor}-{action}')
    for actor, statuses in ROLE_MATRIX.items()
    for action, expected in zip(ACTIONS, statuses)
]


def _state():
    return (
        Transaction.objects.count(),
        Sale.objects.count(),
        sorted(Wallet.objects.values_list('id', 'balance')),
        sorted((b.user_id, bytes(b.encrypted_embedding)) for b in UserBiometric.objects.all()),
    )


def _keys(data):
    """Every dictionary key found anywhere in a response body."""
    if isinstance(data, dict):
        found = set(data)
        for value in data.values():
            found |= _keys(value)
        return found
    if isinstance(data, (list, tuple)):
        return set().union(*(_keys(item) for item in data)) if data else set()
    return set()


@pytest.mark.parametrize('actor, action, expected', MATRIX_CASES)
def test_role_matrix_across_domains(
    actor, action, expected, security_world, client_for, photo, store_face, mock_celery_delay,
):
    """SEC-ISO-01: a permission of one domain never opens another one (role x domain)."""
    vector = store_face(security_world.family_a.student_user, seed=1)
    client = client_for(attrgetter(actor)(security_world))
    before = _state()

    with mock.patch.object(BiometricValidationMixin, 'extract_face_embedding', return_value=vector):
        response = ACTIONS[action](security_world, client, photo)

    assert response.status_code == expected, response.data
    if expected >= 400:
        assert _state() == before
        mock_celery_delay.recharge.assert_not_called()


def test_other_parent_cannot_use_a_foreign_wallet(security_world, client_for, mock_celery_delay):
    """SEC-ISO-02: a parent cannot read or recharge the wallet of another parent's child, in any sede."""
    wallet_a = security_world.family_a.wallet
    before = _state()
    for intruder in (security_world.family_a2.parent, security_world.family_b.parent):
        client = client_for(intruder)
        listing = client.get(f'/api/wallet/{wallet_a.id}/transactions/')
        recharge = client.post(
            '/api/wallet/recharge/',
            {'wallet_id': wallet_a.id, 'amount': '20.00', 'kushki_token': 'tok-1'}, format='json',
        )
        assert (listing.status_code, recharge.status_code) == (403, 403)

    assert _state() == before
    mock_celery_delay.recharge.assert_not_called()


def test_faces_do_not_cross_institutions(security_world, client_for, photo, store_face):
    """SEC-ISO-03: the face of a student of sede B is never matched by POS staff of sede A."""
    world = security_world
    store_face(world.family_a.student_user, seed=1)
    vector_b = store_face(world.family_b.student_user, seed=2)

    def identify(user, query=''):
        with mock.patch.object(BiometricValidationMixin, 'extract_face_embedding', return_value=vector_b):
            return client_for(user).post(
                f'/api/pos/identify/face/{query}', {'photo': photo()}, format='multipart',
            )

    own_sede = identify(world.operator_b)
    assert own_sede.status_code == 200
    assert own_sede.data['user_id'] == world.family_b.student_user.id
    # Control above proves the vector matches; the denials below are the isolation.
    assert identify(world.operator_a).status_code == 404
    assert identify(world.admin_a).status_code == 404
    # Only an admin can widen the scope, and only explicitly.
    widened = identify(world.admin_a, f'?institution={world.inst_b.id}')
    assert widened.status_code == 200
    assert widened.data['user_id'] == world.family_b.student_user.id


def test_cashier_cannot_charge_a_student_of_another_sede(security_world, client_for):
    """SEC-ISO-04: POS staff only charges customers of their own sede."""
    world = security_world
    client = client_for(world.operator_a)
    payload = {
        'payment_method': 'WALLET', 'items': [{'menu_item_id': world.item_a.id, 'quantity': 1}],
    }

    own = client.post('/api/pos/sales/', {**payload, 'user_id': world.family_a.student_user.id}, format='json')
    assert own.status_code == 201, own.data
    before = _state()

    foreign = client.post('/api/pos/sales/', {**payload, 'user_id': world.family_b.student_user.id}, format='json')

    assert foreign.status_code == 400
    assert 'user_id' in foreign.data
    assert _state() == before


def test_access_token_is_not_accepted_as_qr_token(security_world, client_for):
    """SEC-ISO-05: a session JWT cannot impersonate a student at the POS as a QR code."""
    session_token = str(AccessToken.for_user(security_world.family_a.student_user))

    response = client_for(security_world.operator_a).post(
        '/api/pos/identify/qr/', {'token': session_token}, format='json',
    )

    assert response.status_code == 400
    assert 'user_id' not in response.data


def test_responses_never_expose_biometric_data(security_world, client_for, photo, store_face):
    """SEC-ISO-06: payment, profile and POS responses carry no embedding or ciphertext."""
    world = security_world
    vector = store_face(world.family_a.student_user, seed=1)
    stored = UserBiometric.objects.get(user=world.family_a.student_user)
    ciphertext_hex = bytes(stored.encrypted_embedding).hex()
    parent = client_for(world.family_a.parent)
    operator = client_for(world.operator_a)

    with mock.patch.object(BiometricValidationMixin, 'extract_face_embedding', return_value=vector):
        responses = [
            parent.get('/api/users/me/'),
            parent.get(f'/api/wallet/{world.family_a.wallet.id}/transactions/'),
            operator.post('/api/pos/identify/face/', {'photo': photo()}, format='multipart'),
            operator.post(
                '/api/pos/identify/qr/', {'token': build_qr_token(world.family_a.student_user)[0]},
                format='json',
            ),
        ]

    for response in responses:
        assert response.status_code == 200, response.data
        assert not _keys(response.data) & BIOMETRIC_KEYS
        assert ciphertext_hex not in response.content.decode()


def test_biometric_registration_response_has_no_payment_data(security_world, client_for, photo, store_face):
    """SEC-ISO-07: registering a face answers with identity only, never balance or payments."""
    world = security_world
    vector = store_face(world.family_a.student_user, seed=1)

    with mock.patch.object(BiometricValidationMixin, 'extract_face_embedding', return_value=vector):
        response = client_for(world.family_a.parent).post(
            '/api/users/biometrics/register/',
            {'photo': photo(), 'target_user_id': world.family_a.student_user.id}, format='multipart',
        )

    assert response.status_code == 201, response.data
    assert not _keys(response.data) & (PAYMENT_KEYS | BIOMETRIC_KEYS)
