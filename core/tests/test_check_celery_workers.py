from io import StringIO
from unittest import mock

import pytest
from django.core import mail
from django.core.management import call_command

from core import health
from users.models import CustomUser

pytestmark = pytest.mark.django_db


def run(check):
    out = StringIO()
    with mock.patch.object(health, 'check_workers', return_value=check):
        call_command('check_celery_workers', stdout=out, stderr=out)
    return out.getvalue()


@pytest.fixture
def superadmin():
    return CustomUser.objects.create_superuser(username='root', email='root@x.com', password='x')


def test_no_mail_while_workers_listen(superadmin):
    output = run(health.Check('Workers de Celery', health.OK, '1 worker(s)'))

    assert 'OK' in output
    assert mail.outbox == []


def test_superadmins_are_mailed_when_no_worker_listens(superadmin):
    CustomUser.objects.create_superuser(username='root2', email='', password='x')

    run(health.Check('Workers de Celery', health.ERROR, 'Sin workers escuchando la cola «celery»'))

    [sent] = mail.outbox
    assert sent.to == ['root@x.com']
    assert 'Sin workers escuchando' in sent.body
