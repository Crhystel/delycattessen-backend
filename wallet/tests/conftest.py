from decimal import Decimal

import pytest

from users.models import CustomUser, Institution, ParentProfile, StudentProfile
from wallet.models import Wallet


@pytest.fixture
def institution(db):
    return Institution.objects.create(name='Test Institution')


@pytest.fixture
def parent_profile(db):
    parent_user = CustomUser.objects.create_user(
        username='parent1', email='parent1@test.com', password='test1234',
        role=CustomUser.Role.PARENT, first_name='Ana', last_name='Lopez',
    )
    return ParentProfile.objects.create(user=parent_user)


@pytest.fixture
def student_profile(db, institution, parent_profile):
    student_user = CustomUser.objects.create_user(
        username='student1', email='student1@test.com', password='test1234',
        role=CustomUser.Role.STUDENT, first_name='Carlos', last_name='Lopez',
    )
    return StudentProfile.objects.create(
        user=student_user, institution=institution, parent=parent_profile,
    )


@pytest.fixture
def wallet(student_profile):
    return Wallet.objects.create(
        student=student_profile,
        balance=Decimal('10.00'),
        low_balance_threshold=Decimal('2.00'),
    )