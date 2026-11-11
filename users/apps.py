import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class UsersConfig(AppConfig):
    name = 'users'

    def ready(self):
        from celery.signals import task_failure

        from . import alerts  # noqa: F401 - registers the superadmin alert providers

        # Failed e-mails and background jobs used to vanish: results are not stored
        # (CELERY_TASK_IGNORE_RESULT) and the task arguments hold temporary passwords.
        task_failure.connect(record_task_failure, weak=False, dispatch_uid='users.record_task_failure')


def record_task_failure(sender=None, exception=None, **kwargs):
    """Celery `task_failure` receiver: logs the task name and the kind of error only
    (never the arguments) in the audit log. It must not raise inside the worker."""
    try:
        from .audit import record_audit
        from .models import AuditLog

        record_audit(
            AuditLog.Action.TASK_FAILED,
            source=AuditLog.Source.SYSTEM,
            detail={
                'task': getattr(sender, 'name', str(sender)),
                'exception': type(exception).__name__,
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception('Could not record the failed task')
