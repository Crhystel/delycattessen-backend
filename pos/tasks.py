from celery import shared_task


@shared_task
def build_teacher_credit_export_task(export_id: int) -> None:
    """Builds the Excel of the teachers' credit report in the background (HU-08.2)."""
    from .services import run_teacher_credit_export

    run_teacher_credit_export(export_id)


@shared_task(bind=True, max_retries=3, acks_late=True, reject_on_worker_lost=True)
def send_credit_statement_email(self, statement_id: int) -> None:
    """Emails the credit statement of one teacher and month. Only the id travels in the task, never
    the amounts. Retried only on delivery failures (SMTP down, network); after the last attempt
    the statement is marked FAILED so the administrator sees it and can send it again."""
    from smtplib import SMTPException

    from .services import deliver_credit_statement, fail_credit_statement

    try:
        deliver_credit_statement(statement_id)
    except (SMTPException, OSError) as exc:
        if self.request.retries >= self.max_retries:
            fail_credit_statement(statement_id)
            raise
        raise self.retry(exc=exc, countdown=30 * 2 ** self.request.retries)
