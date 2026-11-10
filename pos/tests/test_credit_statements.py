from decimal import Decimal
from smtplib import SMTPException
from unittest import mock

from django.core import mail
from django.test import TestCase
from kombu.exceptions import OperationalError
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from pos.models import CreditStatement
from pos.services import closed_credit_month, deliver_credit_statement, send_credit_statements
from pos.tasks import send_credit_statement_email
from pos.tests.test_analytics import local
from pos.tests.test_teacher_credit_report import TeacherCreditFixtureMixin
from users.models import CustomUser

STATEMENTS = '/api/pos/reports/teacher-credit/statements/'
REPORT = '/api/pos/reports/teacher-credit/'
APRIL = local(2026, 4, 5, 10)  # March is closed, April is in progress
DELAY = 'pos.tasks.send_credit_statement_email.delay'


class CreditStatementFixtureMixin(TeacherCreditFixtureMixin):
    """March 2026 credit sales (see TeacherCreditFixtureMixin), seen from April 5: LUIS MORA VERA
    consumed $9.00 in Sede A (the Feb 28 sale belongs to February) and ANA RUIZ $4.00 in Sede B."""

    def setUp(self):
        super().setUp()
        self.admin_a = CustomUser.objects.create_user(
            username='admin_a', email='aa@x.com', password='x', role=CustomUser.Role.ADMIN, institution=self.inst_a)

    def _auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _send(self, query='?month=2026-03&institution=all', body=None):
        self._auth(self.admin_a)
        with mock.patch(DELAY) as delay, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f'{STATEMENTS}{query}', body or {}, format='json')
        return response, delay


@mock.patch('pos.services.local_now', return_value=APRIL)
class CreditStatementApiTests(CreditStatementFixtureMixin, APITestCase):
    def test_the_month_in_progress_a_future_month_or_none_are_rejected(self, _now):
        for query in ('?month=2026-04', '?month=2026-05', '?month=2027-01', '', '?month=2026-4', '?month=abc'):
            response, delay = self._send(query)
            self.assertEqual(response.status_code, 400, query)
            delay.assert_not_called()
        self.assertFalse(CreditStatement.objects.exists())

    def test_a_closed_month_queues_one_statement_per_teacher(self, _now):
        response, delay = self._send()
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(response.data, {'queued': 2, 'already_sent': 0, 'without_email': 0})
        statements = CreditStatement.objects.order_by('teacher__last_name')
        self.assertEqual(
            [(s.teacher, s.institution, s.month, s.status, s.sent_by) for s in statements],
            [(self.teacher, self.inst_a, '2026-03', 'QUEUED', self.admin_a),
             (self.teacher_b, self.inst_b, '2026-03', 'QUEUED', self.admin_a)],
        )
        self.assertEqual({call.args[0] for call in delay.call_args_list}, {s.pk for s in statements})

    def test_by_default_only_the_admins_own_institution_is_mailed(self, _now):
        response, _delay = self._send('?month=2026-03')
        self.assertEqual(response.data['queued'], 1)
        self.assertEqual(CreditStatement.objects.get().teacher, self.teacher)

    def test_a_double_click_does_not_mail_twice_and_resend_does(self, _now):
        self._send()
        response, delay = self._send()
        self.assertEqual(response.data, {'queued': 0, 'already_sent': 2, 'without_email': 0})
        delay.assert_not_called()
        response, delay = self._send(body={'resend': True})
        self.assertEqual(response.data['queued'], 2)
        self.assertEqual(delay.call_count, 2)
        self.assertEqual(CreditStatement.objects.count(), 2)

    def test_a_failed_statement_is_retried_without_resend(self, _now):
        self._send()
        CreditStatement.objects.filter(teacher=self.teacher).update(status=CreditStatement.Status.FAILED)
        response, delay = self._send()
        self.assertEqual(response.data, {'queued': 1, 'already_sent': 1, 'without_email': 0})
        delay.assert_called_once()

    def test_a_teacher_without_email_is_skipped(self, _now):
        CustomUser.objects.filter(pk=self.teacher.pk).update(email=None)
        response, _delay = self._send()
        self.assertEqual(response.data, {'queued': 1, 'already_sent': 0, 'without_email': 1})
        self.assertEqual(CreditStatement.objects.get().teacher, self.teacher_b)

    def test_an_unreachable_broker_marks_the_statement_failed_without_a_500(self, _now):
        self._auth(self.admin_a)
        with mock.patch(DELAY, side_effect=OperationalError('down')), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f'{STATEMENTS}?month=2026-03', {}, format='json')
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(CreditStatement.objects.get().status, CreditStatement.Status.FAILED)

    def test_only_the_administrator_can_send_them(self, _now):
        self.assertEqual(self.client.post(f'{STATEMENTS}?month=2026-03').status_code, 401)
        for user in (self.cashier, self.teacher):
            self._auth(user)
            self.assertEqual(self.client.post(f'{STATEMENTS}?month=2026-03').status_code, 403)

    def test_the_report_shows_who_already_got_the_statement(self, _now):
        self._send('?month=2026-03&institution=all')
        deliver_credit_statement(CreditStatement.objects.get(teacher=self.teacher).pk)
        rows = {row['full_name']: row for row in self.client.get(f'{REPORT}?month=2026-03&institution=all').data['teachers']}
        self.assertEqual(rows['LUIS MORA VERA']['statement_status'], 'SENT')
        self.assertIsNotNone(rows['LUIS MORA VERA']['statement_sent_at'])
        self.assertEqual(rows['ANA RUIZ']['statement_status'], 'QUEUED')
        self.assertIsNone(rows['ANA RUIZ']['statement_sent_at'])
        current = self.client.get(f'{REPORT}?institution=all').data['teachers']
        self.assertTrue(all(row['statement_status'] is None for row in current))


class ClosedCreditMonthTests(TestCase):
    def test_a_month_closes_at_local_midnight(self):
        with mock.patch('pos.services.local_now', return_value=local(2026, 3, 31, 23, 59)):
            with self.assertRaises(ValidationError):
                closed_credit_month('2026-03')
        with mock.patch('pos.services.local_now', return_value=local(2026, 4, 1, 0, 0)):
            start, end = closed_credit_month('2026-03')
        self.assertEqual((start, end.day), (local(2026, 3, 1, 0), 31))


@mock.patch('pos.services.local_now', return_value=APRIL)
class DeliverCreditStatementTests(CreditStatementFixtureMixin, TestCase):
    def _queue(self, teacher=None):
        with mock.patch(DELAY), self.captureOnCommitCallbacks(execute=True):
            send_credit_statements(self.admin_a, None, '2026-03')
        return CreditStatement.objects.get(teacher=teacher or self.teacher)

    def test_the_email_has_the_total_and_only_that_teachers_consumptions_of_the_month(self, _now):
        statement = self._queue()
        mail.outbox.clear()
        deliver_credit_statement(statement.pk)

        [message] = mail.outbox
        self.assertEqual(message.to, ['t@x.com'])
        self.assertIn('marzo de 2026', message.subject)
        self.assertIn('Total a descontar: $9.00', message.body)
        html = message.alternatives[0][0]
        for expected in ('Hola, LUIS', 'marzo de 2026', 'Sede A', '$9.00', '2 × Sandwich ($2.50 c/u)',
                         '1 × Combo almuerzo ($3.50 c/u)', '01/03/2026', '18/03/2026', '$5.00', '$3.00'):
            self.assertIn(expected, html)  # amounts with a dot, whatever the locale
        self.assertNotIn('5,00', html)
        for unexpected in ('28/02/2026', 'ANA', 'tb@x.com', '$4.00'):  # February and another teacher
            self.assertNotIn(unexpected, html + message.body)
        statement.refresh_from_db()
        self.assertEqual((statement.status, statement.total), ('SENT', Decimal('9.00')))
        self.assertEqual(statement.sent_at, APRIL)

    def test_a_sent_statement_is_not_mailed_again(self, _now):
        statement = self._queue()
        deliver_credit_statement(statement.pk)
        mail.outbox.clear()
        deliver_credit_statement(statement.pk)
        self.assertEqual(mail.outbox, [])

    def test_a_teacher_who_lost_the_email_is_marked_failed(self, _now):
        statement = self._queue()
        CustomUser.objects.filter(pk=self.teacher.pk).update(email=None)
        mail.outbox.clear()
        deliver_credit_statement(statement.pk)
        statement.refresh_from_db()
        self.assertEqual((statement.status, mail.outbox), ('FAILED', []))

    def test_delivery_errors_are_retried_and_end_in_failed(self, _now):
        statement = self._queue()
        with mock.patch('pos.services.deliver_credit_statement', side_effect=SMTPException('down')) as deliver:
            send_credit_statement_email.apply(args=[statement.pk])
        self.assertEqual(deliver.call_count, send_credit_statement_email.max_retries + 1)
        statement.refresh_from_db()
        self.assertEqual(statement.status, CreditStatement.Status.FAILED)
