from datetime import date

from django.test import override_settings
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from core.timezone import local_now
from pos.models import Sale
from pos.tests.test_analytics import AnalyticsFixtureMixin, local
from users.models import CustomUser

SUMMARY = '/api/pos/analytics/summary/'
TIMESERIES = '/api/pos/analytics/timeseries/'
TOP_PRODUCTS = '/api/pos/analytics/top-products/'
TOP_COMBOS = '/api/pos/analytics/top-combos/'
RANGE = 'date_from=2026-03-06&date_to=2026-03-10'


class AnalyticsApiTests(AnalyticsFixtureMixin, APITestCase):
    def setUp(self):
        super().setUp()
        make = CustomUser.objects.create_user
        self.admin_a = make(username='admin_a', email='aa@x.com', password='x',
                            role=CustomUser.Role.ADMIN, institution=self.inst_a)
        self._sale(self.inst_a, Sale.PaymentMethod.CASH, local(2026, 3, 7), [(self.sandwich, 2, '2.50')])
        self._sale(self.inst_b, Sale.PaymentMethod.CASH, local(2026, 3, 8), amount='99.00')
        self._auth(self.admin_a)

    def _auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _revenue(self, query=''):
        response = self.client.get(f'{SUMMARY}?{RANGE}{query}')
        self.assertEqual(response.status_code, 200, response.data)
        return response.data['kpis']['revenue']['current']

    # ---- access ---------------------------------------------------------
    def test_anonymous_gets_401(self):
        self.client.credentials()
        for url in (SUMMARY, TIMESERIES, TOP_PRODUCTS, TOP_COMBOS):
            self.assertEqual(self.client.get(url).status_code, 401)

    def test_only_the_administrator_can_read_analytics(self):
        parent = CustomUser.objects.get(username='parent')
        for user in (self.cashier, self.teacher, parent, self.student_a.user):
            self._auth(user)
            for url in (SUMMARY, TIMESERIES, TOP_PRODUCTS, TOP_COMBOS):
                self.assertEqual(self.client.get(f'{url}?{RANGE}').status_code, 403, (user.role, url))

    # ---- institution scope ----------------------------------------------
    def test_admin_sees_only_own_institution_by_default(self):
        self.assertEqual(self._revenue(), '5.00')

    def test_admin_can_widen_scope_to_another_institution_or_all(self):
        self.assertEqual(self._revenue(f'&institution={self.inst_b.id}'), '99.00')
        self.assertEqual(self._revenue('&institution=all'), '104.00')

    def test_invalid_institution_is_rejected(self):
        self.assertEqual(self.client.get(f'{SUMMARY}?{RANGE}&institution=abc').status_code, 400)

    # ---- query validation -----------------------------------------------
    def test_defaults_to_the_current_month(self):
        response = self.client.get(SUMMARY)
        self.assertEqual(response.status_code, 200, response.data)
        today = local_now().date()
        self.assertEqual(response.data['period']['date_to'], today)
        self.assertEqual(response.data['period']['date_from'], today.replace(day=1))

    def test_start_after_end_is_rejected(self):
        response = self.client.get(f'{SUMMARY}?date_from=2026-03-10&date_to=2026-03-01')
        self.assertEqual(response.status_code, 400)
        self.assertIn('date_from', response.data)

    @override_settings(ANALYTICS_MAX_RANGE_DAYS=10)
    def test_range_longer_than_the_limit_is_rejected(self):
        self.assertEqual(self.client.get(f'{SUMMARY}?date_from=2026-03-01&date_to=2026-03-10').status_code, 200)
        response = self.client.get(f'{SUMMARY}?date_from=2026-03-01&date_to=2026-03-11')
        self.assertEqual(response.status_code, 400)
        self.assertIn('date_to', response.data)

    def test_invalid_date_and_granularity_are_rejected(self):
        self.assertEqual(self.client.get(f'{SUMMARY}?date_from=nope').status_code, 400)
        self.assertEqual(self.client.get(f'{TIMESERIES}?{RANGE}&granularity=year').status_code, 400)

    # ---- payloads -------------------------------------------------------
    def test_timeseries_defaults_to_daily_buckets(self):
        response = self.client.get(f'{TIMESERIES}?{RANGE}')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data), 5)
        self.assertEqual(response.data[1]['bucket_start'], date(2026, 3, 7))
        self.assertEqual(response.data[1]['revenue'], '5.00')
        self.assertEqual(set(response.data[1]['channels']), {'CASH', 'WALLET', 'CREDIT', 'PREORDER'})

    def test_timeseries_accepts_week_granularity(self):
        response = self.client.get(f'{TIMESERIES}?{RANGE}&granularity=week')
        self.assertEqual([row['bucket_start'] for row in response.data], [date(2026, 3, 2), date(2026, 3, 9)])

    def test_top_products_payload(self):
        response = self.client.get(f'{TOP_PRODUCTS}?{RANGE}')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['by_revenue'], [{'name': 'Sandwich', 'units': 2, 'revenue': '5.00'}])
        self.assertEqual(response.data['by_units'], response.data['by_revenue'])

    def test_a_new_sale_shows_up_on_the_next_request(self):
        self.assertEqual(self._revenue(), '5.00')
        self._sale(self.inst_a, Sale.PaymentMethod.CASH, local(2026, 3, 9), [(self.juice, 3, '1.00')])
        self.assertEqual(self._revenue(), '8.00')
