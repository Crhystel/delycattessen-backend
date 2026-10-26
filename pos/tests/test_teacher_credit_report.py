from datetime import date
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from pos.models import Sale, SaleCombo, SaleItem
from pos.services import teacher_credit_report
from pos.tests.test_analytics import AnalyticsFixtureMixin, local
from users.models import CustomUser
from users.services import soft_delete_account

REPORT = '/api/pos/reports/teacher-credit/'
NOW = local(2026, 3, 20, 15, 0)


class TeacherCreditFixtureMixin(AnalyticsFixtureMixin):
    """Credit sales around March 2026; the report is generated on March 20."""

    def setUp(self):
        super().setUp()
        self.teacher.first_name, self.teacher.last_name = 'LUIS', 'MORA'
        self.teacher.second_last_name = 'VERA'
        self.teacher.save()
        self.teacher_b = CustomUser.objects.create_user(
            username='teacher_b', email='tb@x.com', password='x', role=CustomUser.Role.TEACHER,
            institution=self.inst_b, first_name='ANA', last_name='RUIZ')

        credit = Sale.PaymentMethod.CREDIT
        self._sale(self.inst_a, credit, local(2026, 2, 28, 23, 30), [(self.sandwich, 1, '2.50')])
        self.first = self._sale(self.inst_a, credit, local(2026, 3, 1, 0, 30), [(self.sandwich, 2, '2.50')])
        self._sale(self.inst_a, credit, local(2026, 3, 18), [(self.juice, 3, '1.00')])
        self._sale(self.inst_a, Sale.PaymentMethod.CASH, local(2026, 3, 18), [(self.juice, 9, '1.00')])
        self._sale(self.inst_a, credit, local(2026, 3, 21), [(self.juice, 1, '1.00')])  # after NOW
        self._credit_sale_b(local(2026, 3, 10), '4.00')
        SaleCombo.objects.create(sale=self.first, name='Combo almuerzo', quantity=1, price_at_purchase=Decimal('3.50'))

    def _credit_sale_b(self, when, amount):
        sale = Sale.objects.create(
            institution=self.inst_b, cashier=self.cashier, customer=self.teacher_b,
            payment_method=Sale.PaymentMethod.CREDIT, total_amount=Decimal(amount))
        SaleItem.objects.create(sale=sale, menu_item=self.item_b, quantity=4, price_at_purchase=Decimal('1.00'))
        Sale.objects.filter(pk=sale.pk).update(created_at=when)


class TeacherCreditReportTests(TeacherCreditFixtureMixin, TestCase):
    def test_only_credit_sales_from_day_one_until_now(self):
        report = teacher_credit_report(self.inst_a, now=NOW)
        self.assertEqual((report['date_from'], report['date_to']), (date(2026, 3, 1), date(2026, 3, 20)))
        [row] = report['teachers']
        self.assertEqual(row['full_name'], 'LUIS MORA VERA')
        self.assertEqual(len(row['sales']), 2)
        self.assertEqual(row['total'], Decimal('8.00'))
        self.assertEqual(report['totals'], {'teachers': 1, 'consumptions': 2, 'amount': Decimal('8.00')})

    def test_day_one_is_cut_in_local_time(self):
        first_sale = teacher_credit_report(self.inst_a, now=NOW)['teachers'][0]['sales'][0]
        self.assertEqual(first_sale, self.first)

    def test_other_institution_is_excluded_and_none_consolidates(self):
        self.assertEqual(
            [row['full_name'] for row in teacher_credit_report(self.inst_b, now=NOW)['teachers']], ['ANA RUIZ'])
        report = teacher_credit_report(None, now=NOW)
        self.assertEqual([row['full_name'] for row in report['teachers']], ['ANA RUIZ', 'LUIS MORA VERA'])
        self.assertEqual(report['totals'], {'teachers': 2, 'consumptions': 3, 'amount': Decimal('12.00')})

    def test_deactivated_teacher_still_appears(self):
        soft_delete_account(self.teacher)
        [row] = teacher_credit_report(self.inst_a, now=NOW)['teachers']
        self.assertEqual((row['full_name'], row['teacher'].email), ('LUIS MORA VERA', None))

    def test_empty_month(self):
        report = teacher_credit_report(self.inst_a, now=local(2026, 5, 10))
        self.assertEqual(report['teachers'], [])
        self.assertEqual(report['totals'], {'teachers': 0, 'consumptions': 0, 'amount': Decimal('0.00')})


@mock.patch('pos.services.local_now', return_value=NOW)
class TeacherCreditReportApiTests(TeacherCreditFixtureMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.admin_a = CustomUser.objects.create_user(
            username='admin_a', email='aa@x.com', password='x', role=CustomUser.Role.ADMIN, institution=self.inst_a)

    def _auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def test_admin_gets_rows_with_each_sale_and_its_lines(self, _now):
        self._auth(self.admin_a)
        response = self.client.get(REPORT)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data['date_from'], response.data['date_to']), ('2026-03-01', '2026-03-20'))
        [row] = response.data['teachers']
        self.assertEqual(
            (row['full_name'], row['email'], row['institution_name'], row['consumptions'], row['total']),
            ('LUIS MORA VERA', 't@x.com', 'Sede A', 2, '8.00'),
        )
        names = [item['menu_item_name'] for item in row['sales'][0]['items']]
        self.assertEqual(names, ['Sandwich', 'Combo almuerzo'])
        self.assertEqual(response.data['totals'], {'teachers': 1, 'consumptions': 2, 'amount': '8.00'})

    def test_admin_can_consolidate_or_pick_institution(self, _now):
        self._auth(self.admin_a)
        consolidated = self.client.get(f'{REPORT}?institution=all').data
        self.assertEqual(consolidated['totals']['teachers'], 2)
        other = self.client.get(f'{REPORT}?institution={self.inst_b.id}').data
        self.assertEqual([row['full_name'] for row in other['teachers']], ['ANA RUIZ'])

    def test_only_the_administrator_can_read_it(self, _now):
        self.assertEqual(self.client.get(REPORT).status_code, 401)
        for user in (self.cashier, self.teacher):
            self._auth(user)
            self.assertEqual(self.client.get(REPORT).status_code, 403)
