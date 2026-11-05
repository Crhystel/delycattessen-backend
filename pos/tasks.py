from celery import shared_task


@shared_task
def build_teacher_credit_export_task(export_id: int) -> None:
    """Builds the Excel of the teachers' credit report in the background (HU-08.2)."""
    from .services import run_teacher_credit_export

    run_teacher_credit_export(export_id)
