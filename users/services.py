from django.db import transaction
from django.utils import timezone

from .models import UserBiometric


@transaction.atomic
def soft_delete_account(user):
    """
    Soft delete shared by every role. The row stays so the Sale / Transaction
    history (PROTECT foreign keys) and the reports keep the person's name, but
    the account can no longer be used and its personal data is erased:
    - email and username are freed (the address can be registered again),
    - the password becomes unusable,
    - facial biometrics and allergies are deleted.
    Names, role and institution are kept on purpose to audit reports.
    """
    user.is_active = False
    user.deleted_at = timezone.now()
    user.email = None
    user.username = f'deleted-{user.pk}'
    user.must_change_password = False
    user.set_unusable_password()
    user.save(update_fields=[
        'is_active', 'deleted_at', 'email', 'username', 'must_change_password', 'password',
    ])
    UserBiometric.objects.filter(user=user).delete()
    user.allergies.clear()
    return user