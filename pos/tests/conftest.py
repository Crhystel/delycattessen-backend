"""Shared world for the security and isolation tests (SCRUM-29)."""
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from catalog.models import MenuItem
from core.biometrics import BiometricValidationMixin
from users.models import CustomUser, Institution, ParentProfile, StudentProfile, UserBiometric
from wallet.models import Wallet

PASSWORD = 'test1234'
GIF = (
    b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04'
    b'\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
)


def _family(tag, institution):
    """A parent with one student who owns a wallet."""
    make = CustomUser.objects.create_user
    parent_user = make(
        username=f'parent_{tag}', email=f'parent_{tag}@test.com', password=PASSWORD,
        role=CustomUser.Role.PARENT,
    )
    parent = ParentProfile.objects.create(user=parent_user)
    student_user = make(
        username=f'student_{tag}', email=f'student_{tag}@test.com', password=PASSWORD,
        role=CustomUser.Role.STUDENT,
    )
    student = StudentProfile.objects.create(user=student_user, institution=institution, parent=parent)
    wallet = Wallet.objects.create(
        student=student, balance=Decimal('10.00'), low_balance_threshold=Decimal('2.00'),
    )
    return SimpleNamespace(parent=parent_user, student_user=student_user, student=student, wallet=wallet)


@pytest.fixture
def security_world(db):
    """Two institutions; sede A has two families, sede B has one."""
    make = CustomUser.objects.create_user
    inst_a = Institution.objects.create(name='Sede A')
    inst_b = Institution.objects.create(name='Sede B')

    def staff(username, role, institution):
        return make(
            username=username, email=f'{username}@test.com', password=PASSWORD,
            role=role, institution=institution,
        )

    return SimpleNamespace(
        inst_a=inst_a,
        inst_b=inst_b,
        admin_a=staff('admin_a', CustomUser.Role.ADMIN, inst_a),
        teacher_a=staff('teacher_a', CustomUser.Role.TEACHER, inst_a),
        operator_a=staff('operator_a', CustomUser.Role.OPERATIONS_STAFF, inst_a),
        operator_b=staff('operator_b', CustomUser.Role.OPERATIONS_STAFF, inst_b),
        family_a=_family('a', inst_a),
        family_a2=_family('a2', inst_a),
        family_b=_family('b', inst_b),
        item_a=MenuItem.objects.create(institution=inst_a, name='Item A', price=Decimal('1.00'), stock=5),
    )


@pytest.fixture
def client_for():
    """API client authenticated with a genuine access token of `user`."""
    def build(user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
        return client
    return build


@pytest.fixture
def photo():
    """Factory of a tiny image upload (a new file object per call)."""
    return lambda: SimpleUploadedFile('face.gif', GIF, content_type='image/gif')


@pytest.fixture
def store_face():
    """Stores an encrypted face vector for a student and returns the vector."""
    def store(student_user, seed):
        vector = np.random.default_rng(seed).normal(size=128).astype(np.float32)
        vector /= np.linalg.norm(vector)
        ciphertext, nonce, tag = BiometricValidationMixin().encrypt_embedding(vector)
        UserBiometric.objects.create(
            user=student_user, encrypted_embedding=ciphertext, nonce=nonce, tag=tag, is_active=True,
        )
        return vector
    return store
