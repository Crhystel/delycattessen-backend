import io
import os
import tempfile
import zipfile
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from core.timezone import local_now
from pos.models import ReportExport, Sale
from pos.services import TeacherCreditExcelBuilder, run_teacher_credit_export
from pos.tests.test_teacher_credit_report import NOW, TeacherCreditFixtureMixin
from users.models import CustomUser

EXPORTS = '/api/pos/reports/teacher-credit/exports/'
DELAY = 'pos.services.build_teacher_credit_export_task.delay'


def workbook_xml(content):
    """Every XML part of an .xlsx joined, to assert on sheets and cells without extra libraries."""
    with zipfile.ZipFile(content) as archive:
        return {name: archive.read(name).decode('utf-8') for name in archive.namelist() if name.endswith('.xml')}


@mock.patch('pos.services.local_now', return_value=NOW)
class TeacherCreditExportTests(TeacherCreditFixtureMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.admin_a = CustomUser.objects.create_user(
            username='admin_a', email='aa@x.com', password='x', role=CustomUser.Role.ADMIN, institution=self.inst_a)
        self.admin_b = CustomUser.objects.create_user(
            username='admin_b', email='ab@x.com', password='x', role=CustomUser.Role.ADMIN, institution=self.inst_b)

    def _auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _request_export(self, url=EXPORTS):
        self._auth(self.admin_a)
        with mock.patch(DELAY) as delay, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url)
        return response, delay

    def test_post_queues_the_task_and_answers_202(self, _now):
        response, delay = self._request_export()
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(response.data['status'], 'PENDING')
        delay.assert_called_once_with(response.data['id'])
        export = ReportExport.objects.get()
        self.assertEqual((export.requested_by, export.institution), (self.admin_a, self.inst_a))

    def test_consolidated_scope_has_no_institution(self, _now):
        self._request_export(f'{EXPORTS}?institution=all')
        self.assertIsNone(ReportExport.objects.get().institution)

    def test_an_unfinished_export_is_reused(self, _now):
        first, _delay = self._request_export()
        second, delay = self._request_export()
        self.assertEqual(second.data['id'], first.data['id'])
        delay.assert_not_called()
        self.assertEqual(ReportExport.objects.count(), 1)

    def test_a_stuck_export_is_failed_and_a_new_one_is_queued(self, _now):
        stuck, _delay = self._request_export()
        ReportExport.objects.filter(pk=stuck.data['id']).update(created_at=NOW - timedelta(hours=1))
        fresh, delay = self._request_export()
        self.assertNotEqual(fresh.data['id'], stuck.data['id'])
        delay.assert_called_once()
        self.assertEqual(ReportExport.objects.get(pk=stuck.data['id']).status, ReportExport.Status.FAILED)

    def test_polling_fails_an_export_no_worker_picked_up(self, _now):
        queued, _delay = self._request_export()
        url = f"{EXPORTS}{queued.data['id']}/"
        self.assertEqual(self.client.get(url).data['status'], 'PENDING')
        ReportExport.objects.filter(pk=queued.data['id']).update(created_at=NOW - timedelta(minutes=5))
        polled = self.client.get(url)
        self.assertEqual(polled.data['status'], 'FAILED')
        self.assertTrue(polled.data['error'])

    def test_old_exports_are_deleted_with_their_file(self, _now):
        old = ReportExport.objects.create(requested_by=self.admin_a, institution=self.inst_a)
        run_teacher_credit_export(old.pk)
        old.refresh_from_db()
        path = old.file.path
        self.assertTrue(os.path.exists(path))
        ReportExport.objects.filter(pk=old.pk).update(created_at=NOW - timedelta(days=30))
        self._request_export()
        self.assertFalse(ReportExport.objects.filter(pk=old.pk).exists())
        self.assertFalse(os.path.exists(path))

    def test_only_the_administrator_can_request_it(self, _now):
        self.assertEqual(self.client.post(EXPORTS).status_code, 401)
        for user in (self.cashier, self.teacher):
            self._auth(user)
            self.assertEqual(self.client.post(EXPORTS).status_code, 403)

    def test_task_builds_a_styled_workbook_the_owner_can_download(self, _now):
        response, _delay = self._request_export(f'{EXPORTS}?institution=all')
        export_id = response.data['id']
        run_teacher_credit_export(export_id)

        status = self.client.get(f'{EXPORTS}{export_id}/')
        self.assertEqual((status.status_code, status.data['status']), (200, 'DONE'))
        download = self.client.get(f'{EXPORTS}{export_id}/download/')
        self.assertEqual(download.status_code, 200)
        self.assertIn('spreadsheetml.sheet', download['Content-Type'])
        self.assertIn('consumo-docentes-consolidado-2026-03-20.xlsx', download['Content-Disposition'])

        parts = workbook_xml(io.BytesIO(b''.join(download.streaming_content)))
        self.assertIn('Resumen', parts['xl/workbook.xml'])
        self.assertIn('Detalle', parts['xl/workbook.xml'])
        summary, detail = parts['xl/worksheets/sheet1.xml'], parts['xl/worksheets/sheet2.xml']
        for text in ('LUIS MORA VERA', 'ANA RUIZ', 'Sede A', 'Sede B'):
            self.assertIn(text, summary)
        self.assertIn('Combo almuerzo', detail)
        self.assertIn('SUM(F11:F12)', summary)

    def test_names_that_look_like_formulas_stay_text(self, _now):
        self.teacher.first_name = '=HYPERLINK("http://evil.test")'
        self.teacher.save()
        export = ReportExport.objects.create(requested_by=self.admin_a, institution=self.inst_a)
        run_teacher_credit_export(export.pk)
        export.refresh_from_db()
        with export.file.open('rb') as handle:
            parts = workbook_xml(handle)
        for sheet in ('sheet1', 'sheet2'):
            self.assertNotIn('<f>HYPERLINK', parts[f'xl/worksheets/{sheet}.xml'])

    def test_a_failure_is_reported_without_internal_details(self, _now):
        export = ReportExport.objects.create(requested_by=self.admin_a, institution=self.inst_a)
        with mock.patch.object(TeacherCreditExcelBuilder, 'build', side_effect=RuntimeError('secret internals')):
            run_teacher_credit_export(export.pk)
        export.refresh_from_db()
        self.assertEqual(export.status, ReportExport.Status.FAILED)
        self.assertNotIn('secret', export.error)
        self.assertTrue(export.error)
        self.assertFalse(export.file)

    def test_the_task_runs_once_per_export(self, _now):
        export = ReportExport.objects.create(requested_by=self.admin_a, institution=self.inst_a)
        run_teacher_credit_export(export.pk)
        with mock.patch.object(TeacherCreditExcelBuilder, 'build') as build:
            run_teacher_credit_export(export.pk)
        build.assert_not_called()

    def test_another_administrator_cannot_read_or_download_it(self, _now):
        response, _delay = self._request_export()
        export_id = response.data['id']
        run_teacher_credit_export(export_id)
        self._auth(self.admin_b)
        self.assertEqual(self.client.get(f'{EXPORTS}{export_id}/').status_code, 403)
        self.assertEqual(self.client.get(f'{EXPORTS}{export_id}/download/').status_code, 403)

    def test_download_before_it_is_ready_is_rejected(self, _now):
        response, _delay = self._request_export()
        self.assertEqual(self.client.get(f"{EXPORTS}{response.data['id']}/download/").status_code, 400)

    def test_unknown_export_is_404(self, _now):
        self._auth(self.admin_a)
        self.assertEqual(self.client.get(f'{EXPORTS}999999/').status_code, 404)


class TeacherCreditExportVolumeTests(TeacherCreditFixtureMixin, APITestCase):
    def test_thousands_of_sales_are_streamed_into_the_workbook(self):
        now = local_now() + timedelta(minutes=1)
        Sale.objects.bulk_create([
            Sale(institution=self.inst_a, cashier=self.cashier, customer=self.teacher,
                 payment_method=Sale.PaymentMethod.CREDIT, total_amount=Decimal('1.00'))
            for _ in range(3000)
        ])
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'report.xlsx')
            TeacherCreditExcelBuilder(self.inst_a, 'Sede A', now).build(path)
            parts = workbook_xml(path)
        detail = parts['xl/worksheets/sheet2.xml']
        self.assertGreaterEqual(detail.count('<row '), 3000)
        self.assertIn('SUM(H8:H3007)', detail)

