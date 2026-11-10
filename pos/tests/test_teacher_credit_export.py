import base64
import io
import os
import re
import tempfile
import zipfile
import zlib
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from core.timezone import local_now
from pos.models import ReportExport, Sale
from pos.services import (
    TeacherCreditExcelBuilder, TeacherCreditPdfBuilder, credit_period, run_teacher_credit_export,
)
from pos.tests.test_teacher_credit_report import NOW, TeacherCreditFixtureMixin
from users.models import CustomUser

EXPORTS = '/api/pos/reports/teacher-credit/exports/'
DELAY = 'pos.services.build_teacher_credit_export_task.delay'


def workbook_xml(content):
    """Every XML part of an .xlsx joined, to assert on sheets and cells without extra libraries."""
    with zipfile.ZipFile(content) as archive:
        return {name: archive.read(name).decode('utf-8') for name in archive.namelist() if name.endswith('.xml')}


def pdf_text(content):
    """Text drawn on every page of a reportlab PDF (its page streams are ASCII85 + Flate)."""
    chunks = []
    for stream in re.findall(rb'stream\r?\n(.*?)endstream', content, re.S):
        data = base64.a85decode(stream.strip().removesuffix(b'~>'), adobe=False)
        chunks.append(zlib.decompress(data).decode('latin-1'))
    return ''.join(chunks)


def pdf_pages(content):
    return len(re.findall(rb'/Type\s*/Page(?!s)', content))


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
        self.assertIn('consumo-docentes-consolidado-2026-03-01_2026-03-20.xlsx', download['Content-Disposition'])

        parts = workbook_xml(io.BytesIO(b''.join(download.streaming_content)))
        for name in ('Resumen', 'Nómina', 'Detalle'):
            self.assertIn(name, parts['xl/workbook.xml'])
        summary, detail = parts['xl/worksheets/sheet1.xml'], parts['xl/worksheets/sheet3.xml']
        for text in ('LUIS MORA VERA', 'ANA RUIZ', 'Sede A', 'Sede B'):
            self.assertIn(text, summary)
        self.assertIn('Combo almuerzo', detail)
        self.assertIn('SUM(F11:F12)', summary)

    def test_payroll_sheet_is_a_flat_table_the_erp_can_import(self, _now):
        response, _delay = self._request_export(f'{EXPORTS}?institution=all')
        run_teacher_credit_export(response.data['id'])
        with ReportExport.objects.get().file.open('rb') as handle:
            parts = workbook_xml(handle)
        summary, payroll = parts['xl/worksheets/sheet1.xml'], parts['xl/worksheets/sheet2.xml']
        self.assertIn('<mergeCell ', summary)
        self.assertNotIn('<mergeCell', payroll)  # no titles or merged cells
        self.assertNotIn('<f>', payroll)  # no total row
        self.assertIn('<row r="1"', payroll)  # headers on the first row
        for text in ('Código', 'Apellidos', 'Nombres', 'Valor a descontar', 'MORA VERA', 'LUIS', 'RUIZ', 'ANA'):
            self.assertIn(text, payroll)
        self.assertEqual(payroll.count('<row '), 3)  # headers + two teachers

    def test_pdf_export_is_built_and_downloaded(self, _now):
        response, _delay = self._request_export(f'{EXPORTS}?institution=all&file_type=pdf')
        self.assertEqual(response.status_code, 202, response.data)
        run_teacher_credit_export(response.data['id'])
        download = self.client.get(f"{EXPORTS}{response.data['id']}/download/")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download['Content-Type'], 'application/pdf')
        self.assertIn('consumo-docentes-consolidado-2026-03-01_2026-03-20.pdf', download['Content-Disposition'])
        content = b''.join(download.streaming_content)
        self.assertTrue(content.startswith(b'%PDF'))
        text = pdf_text(content)
        for expected in ('LUIS MORA VERA', 'ANA RUIZ', '$12.00', 'Subtotal del docente', 'Elaborado por', 'gina 1 de '):
            self.assertIn(expected, text)

    def test_a_past_month_is_exported_complete(self, _now):
        response, _delay = self._request_export(f'{EXPORTS}?month=2026-02')
        run_teacher_credit_export(response.data['id'])
        download = self.client.get(f"{EXPORTS}{response.data['id']}/download/")
        self.assertIn('consumo-docentes-sede-a-2026-02-01_2026-02-28.xlsx', download['Content-Disposition'])
        with ReportExport.objects.get().file.open('rb') as handle:
            summary = workbook_xml(handle)['xl/worksheets/sheet1.xml']
        self.assertIn('01/02/2026 - 28/02/2026', summary)

    def test_a_bad_month_or_file_type_is_rejected_before_queueing(self, _now):
        for query in ('month=2099-01', 'month=2026-13', 'file_type=docx'):
            response, delay = self._request_export(f'{EXPORTS}?{query}')
            self.assertEqual(response.status_code, 400, query)
            delay.assert_not_called()
        self.assertFalse(ReportExport.objects.exists())

    def test_an_unfinished_export_is_reused_only_for_the_same_month_and_type(self, _now):
        first, _delay = self._request_export()
        same, delay = self._request_export()
        self.assertEqual(same.data['id'], first.data['id'])
        delay.assert_not_called()
        for query in ('?file_type=pdf', '?month=2026-02'):
            other, delay = self._request_export(f'{EXPORTS}{query}')
            self.assertNotEqual(other.data['id'], first.data['id'], query)
            delay.assert_called_once()

    def test_pdf_survives_markup_and_unusual_characters_in_names(self, _now):
        self.teacher.first_name = 'A&B <b>Łukasz</b>'
        self.teacher.save()
        for month in ('', '2025-01'):  # with sales, and an empty month
            export = ReportExport.objects.create(
                requested_by=self.admin_a, institution=self.inst_a, month=month, file_type=ReportExport.FileType.PDF)
            run_teacher_credit_export(export.pk)
            export.refresh_from_db()
            self.assertEqual(export.status, ReportExport.Status.DONE, export.error)
            with export.file.open('rb') as handle:
                self.assertEqual(handle.read(4), b'%PDF')

    def test_names_that_look_like_formulas_stay_text(self, _now):
        self.teacher.first_name = '=HYPERLINK("http://evil.test")'
        self.teacher.save()
        export = ReportExport.objects.create(requested_by=self.admin_a, institution=self.inst_a)
        run_teacher_credit_export(export.pk)
        export.refresh_from_db()
        with export.file.open('rb') as handle:
            parts = workbook_xml(handle)
        for sheet in ('sheet1', 'sheet2', 'sheet3'):
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
        self._credit_sales(3000)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'report.xlsx')
            TeacherCreditExcelBuilder(self.inst_a, 'Sede A', credit_period(None, now), now).build(path)
            parts = workbook_xml(path)
        detail = parts['xl/worksheets/sheet3.xml']
        self.assertGreaterEqual(detail.count('<row '), 3000)
        self.assertIn('SUM(J8:J3007)', detail)

    def test_thousands_of_sales_fit_in_a_paginated_pdf(self):
        now = local_now() + timedelta(minutes=1)
        self._credit_sales(3000)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'report.pdf')
            TeacherCreditPdfBuilder(self.inst_a, 'Sede A', credit_period(None, now), now, 'Admin').build(path)
            with open(path, 'rb') as handle:
                content = handle.read()
        self.assertTrue(content.startswith(b'%PDF'))
        self.assertGreater(pdf_pages(content), 20)
        self.assertIn('$3,000.00', pdf_text(content))

    def test_queries_do_not_grow_with_the_number_of_sales(self):
        def build(builder_class, extension):
            now = local_now() + timedelta(minutes=1)
            with tempfile.TemporaryDirectory() as folder, CaptureQueriesContext(connection) as queries:
                builder_class(
                    self.inst_a, 'Sede A', credit_period(None, now), now, 'Admin',
                ).build(os.path.join(folder, f'report.{extension}'))
            return len(queries)

        for builder_class, extension in ((TeacherCreditExcelBuilder, 'xlsx'), (TeacherCreditPdfBuilder, 'pdf')):
            self._credit_sales(20)
            few = build(builder_class, extension)
            self._credit_sales(300)
            self.assertEqual(build(builder_class, extension), few, builder_class.__name__)

    def _credit_sales(self, count):
        Sale.objects.bulk_create([
            Sale(institution=self.inst_a, cashier=self.cashier, customer=self.teacher,
                 payment_method=Sale.PaymentMethod.CREDIT, total_amount=Decimal('1.00'))
            for _ in range(count)
        ])

