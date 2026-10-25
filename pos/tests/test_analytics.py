from datetime import date, datetime
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from catalog.models import DailyMenu, Ingredient, MenuDish, MenuItem, MonthlyMenu
from core.timezone import LOCAL_TIMEZONE
from pos.models import PreOrder, PreOrderItem, Sale, SaleCombo, SaleItem
from pos.services import CHANNEL_PREORDER, SalesAnalytics
from users.models import CustomUser, Institution, ParentProfile, StudentProfile
from wallet.models import Transaction, Wallet

GIF = (
    b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04'
    b'\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
)


def local(year, month, day, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=LOCAL_TIMEZONE)


class AnalyticsFixtureMixin:
    """Two institutions with sales created directly and dated by hand
    (created_at is auto_now_add, so it is moved with update())."""

    def setUp(self):
        make = CustomUser.objects.create_user
        self.inst_a = Institution.objects.create(name='Sede A')
        self.inst_b = Institution.objects.create(name='Sede B')
        self.cashier = make(username='cashier', email='c@x.com', password='x',
                            role=CustomUser.Role.OPERATIONS_STAFF, institution=self.inst_a)
        self.teacher = make(username='teacher', email='t@x.com', password='x',
                            role=CustomUser.Role.TEACHER, institution=self.inst_a)
        parent_user = make(username='parent', email='p@x.com', password='x', role=CustomUser.Role.PARENT)
        parent = ParentProfile.objects.create(user=parent_user)
        self.student_a = self._student(parent, self.inst_a, 'student_a')
        self.student_b = self._student(parent, self.inst_b, 'student_b')

        ingredient = Ingredient.objects.create(name='Base', allergens_reviewed=True)
        self.sandwich = self._item(self.inst_a, 'Sandwich', '2.50', ingredient)
        self.juice = self._item(self.inst_a, 'Jugo', '1.00', ingredient)
        self.item_b = self._item(self.inst_b, 'Pan B', '1.00', ingredient)

    def _student(self, parent, institution, username):
        user = CustomUser.objects.create_user(
            username=username, email=f'{username}@x.com', password='x', role=CustomUser.Role.STUDENT)
        profile = StudentProfile.objects.create(
            user=user, parent=parent, institution=institution,
            profile_picture=SimpleUploadedFile('s.gif', GIF, content_type='image/gif'))
        Wallet.objects.create(student=profile, balance=Decimal('50.00'), low_balance_threshold=Decimal('1.00'))
        return profile

    @staticmethod
    def _item(institution, name, price, ingredient):
        item = MenuItem.objects.create(institution=institution, name=name, price=Decimal(price), stock=100)
        item.ingredients.add(ingredient)
        return item

    def _sale(self, institution, method, when, lines=(), amount=None):
        """`lines` is a list of (menu_item, quantity, unit_price)."""
        total = Decimal(amount) if amount is not None else sum(
            (Decimal(price) * quantity for _item, quantity, price in lines), Decimal('0.00'))
        customer = {
            Sale.PaymentMethod.CASH: None,
            Sale.PaymentMethod.CREDIT: self.teacher,
            Sale.PaymentMethod.WALLET: self.student_a.user,
        }[method]
        sale = Sale.objects.create(
            institution=institution, cashier=self.cashier, customer=customer,
            payment_method=method, total_amount=total)
        for item, quantity, price in lines:
            SaleItem.objects.create(sale=sale, menu_item=item, quantity=quantity, price_at_purchase=Decimal(price))
        Sale.objects.filter(pk=sale.pk).update(created_at=when)
        return sale

    def _dish(self, institution, name, price):
        return MenuDish.objects.create(
            institution=institution, name=name, price=Decimal(price),
            image=SimpleUploadedFile('d.gif', GIF, content_type='image/gif'))

    def _preorder(self, student, when, lines, status=PreOrder.Status.PENDING):
        """`lines` is a list of (dish, quantity, unit_price)."""
        institution = student.institution
        monthly, _created = MonthlyMenu.objects.get_or_create(
            institution=institution, year=2030, month=1, defaults={'status': MonthlyMenu.Status.PUBLISHED})
        daily, _created = DailyMenu.objects.get_or_create(monthly_menu=monthly, date=date(2030, 1, 15))
        total = sum((Decimal(price) * quantity for _dish, quantity, price in lines), Decimal('0.00'))
        pre_order = PreOrder.objects.create(student=student, daily_menu=daily, total_amount=total, status=status)
        for dish, quantity, price in lines:
            PreOrderItem.objects.create(
                pre_order=pre_order, dish=dish, quantity=quantity, price_at_purchase=Decimal(price))
        PreOrder.objects.filter(pk=pre_order.pk).update(created_at=when)
        return pre_order


class SummaryTests(AnalyticsFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        cash, wallet, credit = Sale.PaymentMethod.CASH, Sale.PaymentMethod.WALLET, Sale.PaymentMethod.CREDIT
        # Previous period (Mar 1-5 -> Mar 6-10 is the current one)
        self._sale(self.inst_a, cash, local(2026, 3, 2), amount='10.00')
        # Current period: Mar 6-10
        self._sale(self.inst_a, cash, local(2026, 3, 7), [(self.sandwich, 2, '2.50')])
        self._sale(self.inst_a, wallet, local(2026, 3, 8), [(self.juice, 5, '1.00')])
        self._sale(self.inst_a, credit, local(2026, 3, 9), amount='20.00')
        self._sale(self.inst_b, cash, local(2026, 3, 9), amount='99.00')
        dish = self._dish(self.inst_a, 'Seco', '3.00')
        self._preorder(self.student_a, local(2026, 3, 10), [(dish, 2, '3.00')])

    def _summary(self, institution=None, date_from=date(2026, 3, 6), date_to=date(2026, 3, 10)):
        return SalesAnalytics(institution or self.inst_a, date_from, date_to).summary()

    def test_kpis_compare_against_previous_period_of_same_length(self):
        summary = self._summary()
        self.assertEqual(summary['previous_period'], {'date_from': date(2026, 3, 1), 'date_to': date(2026, 3, 5)})
        self.assertEqual(summary['kpis']['revenue'], {'current': '36.00', 'previous': '10.00', 'change_pct': 260.0})
        self.assertEqual(summary['kpis']['transactions'], {'current': 4, 'previous': 1, 'change_pct': 300.0})
        self.assertEqual(summary['kpis']['average_ticket']['current'], '9.00')

    def test_channels_split_pos_methods_and_preorders(self):
        channels = {row['channel']: row for row in self._summary()['channels']}
        self.assertEqual(
            [(key, channels[key]['amount'], channels[key]['count']) for key in channels],
            [('CASH', '5.00', 1), ('WALLET', '5.00', 1), ('CREDIT', '20.00', 1), (CHANNEL_PREORDER, '6.00', 1)],
        )
        self.assertEqual(channels['CREDIT']['share_pct'], 55.6)
        self.assertEqual(channels['CASH']['previous_amount'], '10.00')
        self.assertEqual(channels['CASH']['change_pct'], -50.0)
        self.assertIsNone(channels['CREDIT']['change_pct'])

    def test_change_is_none_when_previous_period_is_empty(self):
        summary = self._summary(date_from=date(2026, 1, 1), date_to=date(2026, 1, 2))
        self.assertEqual(summary['kpis']['revenue'], {'current': '0.00', 'previous': '0.00', 'change_pct': None})
        self.assertEqual(summary['kpis']['average_ticket']['current'], '0.00')
        self.assertTrue(all(row['share_pct'] == 0.0 for row in summary['channels']))

    def test_other_institution_is_excluded_and_none_consolidates(self):
        self.assertEqual(self._summary(self.inst_b)['kpis']['revenue']['current'], '99.00')
        consolidated = SalesAnalytics(None, date(2026, 3, 6), date(2026, 3, 10)).summary()
        self.assertEqual(consolidated['kpis']['revenue']['current'], '135.00')

    def test_canceled_preorders_do_not_count(self):
        dish = self._dish(self.inst_a, 'Postre', '4.00')
        self._preorder(self.student_a, local(2026, 3, 10), [(dish, 1, '4.00')], status=PreOrder.Status.CANCELED)
        self._preorder(self.student_a, local(2026, 3, 10), [(dish, 1, '4.00')], status=PreOrder.Status.DELIVERED)
        channels = {row['channel']: row for row in self._summary()['channels']}
        self.assertEqual(channels[CHANNEL_PREORDER]['amount'], '10.00')
        self.assertEqual(channels[CHANNEL_PREORDER]['count'], 2)

    def test_preorder_of_another_institution_is_excluded(self):
        dish = self._dish(self.inst_b, 'Plato B', '7.00')
        self._preorder(self.student_b, local(2026, 3, 8), [(dish, 1, '7.00')])
        channels = {row['channel']: row for row in self._summary()['channels']}
        self.assertEqual(channels[CHANNEL_PREORDER]['amount'], '6.00')

    def test_recharges_and_wallet_consumption_are_not_revenue(self):
        wallet = self.student_a.wallet
        Transaction.objects.create(
            wallet=wallet, amount=Decimal('50.00'), gateway='kushki',
            status=Transaction.Status.SUCCESS, type=Transaction.Type.RECHARGE)
        Transaction.objects.create(
            wallet=wallet, amount=Decimal('5.00'),
            status=Transaction.Status.SUCCESS, type=Transaction.Type.CONSUMPTION)
        self.assertEqual(self._summary()['kpis']['revenue']['current'], '36.00')

    def test_day_is_cut_in_local_time_not_utc(self):
        # 23:30 in Ecuador is already the next day in UTC.
        self._sale(self.inst_a, Sale.PaymentMethod.CASH, local(2026, 3, 10, 23, 30), amount='1.00')
        # 00:10 local on the 11th is outside a range that ends on the 10th.
        self._sale(self.inst_a, Sale.PaymentMethod.CASH, local(2026, 3, 11, 0, 10), amount='500.00')
        self.assertEqual(self._summary()['kpis']['revenue']['current'], '37.00')


class TimeseriesTests(AnalyticsFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        cash, wallet = Sale.PaymentMethod.CASH, Sale.PaymentMethod.WALLET
        self._sale(self.inst_a, cash, local(2026, 3, 4), amount='10.00')
        self._sale(self.inst_a, cash, local(2026, 3, 4, 23, 30), amount='2.00')
        self._sale(self.inst_a, wallet, local(2026, 3, 6), amount='5.00')
        self._sale(self.inst_a, cash, local(2026, 3, 18), amount='7.00')
        dish = self._dish(self.inst_a, 'Seco', '3.00')
        self._preorder(self.student_a, local(2026, 3, 6), [(dish, 1, '3.00')])

    def _series(self, granularity, date_from=date(2026, 3, 4), date_to=date(2026, 3, 8)):
        return SalesAnalytics(self.inst_a, date_from, date_to).timeseries(granularity)

    def test_days_without_sales_are_filled_with_zero(self):
        series = self._series('day')
        self.assertEqual([row['bucket_start'] for row in series], [date(2026, 3, day) for day in range(4, 9)])
        self.assertEqual([row['revenue'] for row in series], ['12.00', '0.00', '8.00', '0.00', '0.00'])
        self.assertEqual(series[0]['transactions'], 2)
        self.assertEqual(series[2]['channels'], {'CASH': '0.00', 'WALLET': '5.00', 'CREDIT': '0.00', 'PREORDER': '3.00'})

    def test_late_evening_sale_stays_on_its_local_day(self):
        self.assertEqual(self._series('day')[0]['revenue'], '12.00')
        self.assertEqual(self._series('day')[1]['revenue'], '0.00')

    def test_week_buckets_start_on_monday_and_are_clipped_to_the_range(self):
        series = self._series('week', date_to=date(2026, 3, 18))
        self.assertEqual([row['bucket_start'] for row in series], [date(2026, 3, 2), date(2026, 3, 9), date(2026, 3, 16)])
        self.assertEqual((series[0]['date_from'], series[0]['date_to']), (date(2026, 3, 4), date(2026, 3, 8)))
        self.assertEqual((series[2]['date_from'], series[2]['date_to']), (date(2026, 3, 16), date(2026, 3, 18)))
        self.assertEqual([row['revenue'] for row in series], ['20.00', '0.00', '7.00'])

    def test_month_buckets_span_months_of_the_range(self):
        series = self._series('month', date_from=date(2026, 2, 20), date_to=date(2026, 3, 31))
        self.assertEqual([row['bucket_start'] for row in series], [date(2026, 2, 1), date(2026, 3, 1)])
        self.assertEqual((series[0]['date_from'], series[0]['date_to']), (date(2026, 2, 20), date(2026, 2, 28)))
        self.assertEqual([row['revenue'] for row in series], ['0.00', '27.00'])

    def test_bucket_range_drills_down_to_the_same_numbers(self):
        row = self._series('day')[0]
        summary = SalesAnalytics(self.inst_a, row['date_from'], row['date_to']).summary()
        self.assertEqual(summary['kpis']['revenue']['current'], row['revenue'])
        self.assertEqual(summary['kpis']['transactions']['current'], row['transactions'])


class TopProductsTests(AnalyticsFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        cash = Sale.PaymentMethod.CASH
        # The sandwich was sold at a promotional price in the second sale.
        self._sale(self.inst_a, cash, local(2026, 3, 5), [(self.sandwich, 2, '2.50'), (self.juice, 10, '1.00')])
        self._sale(self.inst_a, cash, local(2026, 3, 6), [(self.sandwich, 1, '2.00')])
        self._sale(self.inst_b, cash, local(2026, 3, 6), [(self.item_b, 50, '1.00')])
        self._sale(self.inst_a, cash, local(2026, 4, 1), [(self.juice, 99, '1.00')])

    def _top(self, institution=None):
        return SalesAnalytics(institution or self.inst_a, date(2026, 3, 1), date(2026, 3, 31)).top_products()

    def test_revenue_uses_the_price_paid_and_ranks_by_revenue_and_units(self):
        top = self._top()
        self.assertEqual(top['by_revenue'], [
            {'name': 'Jugo', 'units': 10, 'revenue': '10.00'},
            {'name': 'Sandwich', 'units': 3, 'revenue': '7.00'},
        ])
        self.assertEqual([row['name'] for row in top['by_units']], ['Jugo', 'Sandwich'])

    def test_dishes_and_items_with_the_same_name_are_merged(self):
        dish = self._dish(self.inst_a, ' sandwich ', '3.00')
        self._preorder(self.student_a, local(2026, 3, 7), [(dish, 4, '3.00')])
        rows = {row['name']: row for row in self._top()['by_revenue']}
        self.assertEqual(rows['Sandwich'], {'name': 'Sandwich', 'units': 7, 'revenue': '19.00'})

    def test_canceled_preorders_and_other_institutions_are_excluded(self):
        dish = self._dish(self.inst_a, 'Seco', '3.00')
        self._preorder(self.student_a, local(2026, 3, 7), [(dish, 4, '3.00')], status=PreOrder.Status.CANCELED)
        self.assertNotIn('Seco', [row['name'] for row in self._top()['by_revenue']])
        self.assertEqual([row['name'] for row in self._top(self.inst_b)['by_revenue']], ['Pan B'])

    @override_settings(ANALYTICS_TOP_PRODUCTS_LIMIT=1)
    def test_ranking_is_limited_by_setting(self):
        top = self._top()
        self.assertEqual(len(top['by_revenue']), 1)
        self.assertEqual(len(top['by_units']), 1)


class TopCombosTests(AnalyticsFixtureMixin, TestCase):
    """Combos are ranked apart from the products, at the price they were sold."""

    def setUp(self):
        super().setUp()
        cash = Sale.PaymentMethod.CASH
        self._combo_sale(self.inst_a, local(2026, 3, 5), [('Combo almuerzo', 2, '3.50'), ('Combo snack', 5, '1.50')])
        self._combo_sale(self.inst_a, local(2026, 3, 6), [('Combo almuerzo', 1, '3.00')])
        self._combo_sale(self.inst_b, local(2026, 3, 6), [('Combo B', 9, '2.00')])
        self._combo_sale(self.inst_a, local(2026, 4, 1), [('Combo almuerzo', 50, '3.50')])
        self._sale(self.inst_a, cash, local(2026, 3, 6), [(self.sandwich, 1, '2.50')])

    def _combo_sale(self, institution, when, lines):
        """`lines` is a list of (combo name, quantity, unit_price)."""
        sale = self._sale(institution, Sale.PaymentMethod.CASH, when, amount=sum(
            (Decimal(price) * quantity for _name, quantity, price in lines), Decimal('0.00')))
        for name, quantity, price in lines:
            SaleCombo.objects.create(sale=sale, name=name, quantity=quantity, price_at_purchase=Decimal(price))
        return sale

    def _top(self, institution=None):
        return SalesAnalytics(institution, date(2026, 3, 1), date(2026, 3, 31)).top_combos()

    def test_combos_are_ranked_with_their_totals(self):
        top = self._top(self.inst_a)
        self.assertEqual(top['by_revenue'], [
            {'name': 'Combo almuerzo', 'units': 3, 'revenue': '10.00'},
            {'name': 'Combo snack', 'units': 5, 'revenue': '7.50'},
        ])
        self.assertEqual([row['name'] for row in top['by_units']], ['Combo snack', 'Combo almuerzo'])
        self.assertEqual(top['totals'], {'units': 8, 'revenue': '17.50'})

    def test_products_are_not_mixed_with_combos(self):
        names = [row['name'] for row in self._top(self.inst_a)['by_revenue']]
        self.assertNotIn('Sandwich', names)
        products = SalesAnalytics(self.inst_a, date(2026, 3, 1), date(2026, 3, 31)).top_products()
        self.assertNotIn('Combo almuerzo', [row['name'] for row in products['by_revenue']])

    def test_institution_scope_and_consolidated_view(self):
        self.assertEqual([row['name'] for row in self._top(self.inst_b)['by_revenue']], ['Combo B'])
        self.assertEqual(self._top()['totals'], {'units': 17, 'revenue': '35.50'})

    def test_empty_period_has_zero_totals(self):
        top = SalesAnalytics(self.inst_a, date(2026, 5, 1), date(2026, 5, 31)).top_combos()
        self.assertEqual(top, {'by_revenue': [], 'by_units': [], 'totals': {'units': 0, 'revenue': '0.00'}})
