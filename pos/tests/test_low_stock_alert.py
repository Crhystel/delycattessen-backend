from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from kombu.exceptions import OperationalError
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from catalog.models import Combo, ComboItem, Ingredient, MenuItem
from catalog.services import ComboService, LowStockService
from catalog.tasks import send_low_stock_email
from users.models import CustomUser, Institution

GIF = (
    b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04'
    b'\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
)
SALES = '/api/pos/sales/'
TODAY = date(2026, 11, 12)
LOCMEM = 'django.core.mail.backends.locmem.EmailBackend'
DELAY = 'catalog.services.send_low_stock_email.delay'


@override_settings(LOW_STOCK_THRESHOLD=5)
class CrossedTests(SimpleTestCase):
    def test_entering_the_low_level_or_running_out_alerts(self):
        self.assertTrue(LowStockService.crossed(6, 5))
        self.assertTrue(LowStockService.crossed(9, 2))
        self.assertTrue(LowStockService.crossed(3, 0))
        self.assertTrue(LowStockService.crossed(8, 0))

    def test_staying_low_or_above_does_not_alert(self):
        self.assertFalse(LowStockService.crossed(5, 4))
        self.assertFalse(LowStockService.crossed(10, 6))
        self.assertFalse(LowStockService.crossed(0, 0))


@override_settings(LOW_STOCK_THRESHOLD=5)
@mock.patch.object(ComboService, 'today', return_value=TODAY)
class LowStockSaleTests(APITestCase):
    def setUp(self):
        self.institution = Institution.objects.create(name='Sede A')
        self.cashier = CustomUser.objects.create_user(
            username='cajero', email='caja@x.com', password='x',
            role=CustomUser.Role.OPERATIONS_STAFF, institution=self.institution,
        )
        ingredient = Ingredient.objects.create(name='Base', allergens_reviewed=True)
        self.sandwich = self._item('Sanduche', 7, ingredient)
        self.cola = self._item('Cola', 20, ingredient)
        self.combo = Combo.objects.create(
            institution=self.institution, name='Combo almuerzo', price=Decimal('3.50'),
            start_date=TODAY, end_date=TODAY + timedelta(days=7),
            image=SimpleUploadedFile('c.gif', GIF, content_type='image/gif'),
        )
        ComboItem.objects.create(combo=self.combo, menu_item=self.sandwich, quantity=1)
        ComboItem.objects.create(combo=self.combo, menu_item=self.cola, quantity=1)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.cashier).access_token}')

    def _item(self, name, stock, ingredient):
        item = MenuItem.objects.create(institution=self.institution, name=name, price=Decimal('1.00'), stock=stock)
        item.ingredients.add(ingredient)
        return item

    def _sell(self, items=(), combos=()):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(SALES, {
                'payment_method': 'CASH',
                'items': [{'menu_item_id': item.id, 'quantity': quantity} for item, quantity in items],
                'combos': [{'combo_id': combo.id, 'quantity': quantity} for combo, quantity in combos],
            }, format='json')
        self.assertEqual(response.status_code, 201, response.data)

    def test_a_sale_that_crosses_the_threshold_alerts_product_and_combo(self, _today):
        with mock.patch(DELAY) as delay:
            self._sell(items=[(self.sandwich, 2)])
        delay.assert_called_once_with(self.institution.id, [self.sandwich.id], [self.combo.id])

    def test_a_sale_that_stays_above_the_threshold_does_not_alert(self, _today):
        with mock.patch(DELAY) as delay:
            self._sell(items=[(self.cola, 3)])
        delay.assert_not_called()

    def test_it_is_quiet_while_low_and_alerts_again_when_sold_out(self, _today):
        with mock.patch(DELAY) as delay:
            self._sell(items=[(self.sandwich, 2)])   # 7 -> 5: low
            self._sell(items=[(self.sandwich, 1)])   # 5 -> 4: still low, quiet
            self._sell(combos=[(self.combo, 4)])     # 4 -> 0: sold out
        self.assertEqual(delay.call_count, 2)
        self.assertEqual(delay.call_args.args, (self.institution.id, [self.sandwich.id], [self.combo.id]))

    def test_an_unreachable_broker_does_not_break_the_sale(self, _today):
        with mock.patch(DELAY, side_effect=OperationalError('down')):
            self._sell(items=[(self.sandwich, 2)])
        self.sandwich.refresh_from_db()
        self.assertEqual(self.sandwich.stock, 5)


@override_settings(EMAIL_BACKEND=LOCMEM, LOW_STOCK_THRESHOLD=5)
class LowStockEmailTests(APITestCase):
    def setUp(self):
        self.institution = Institution.objects.create(name='Sede A')
        other = Institution.objects.create(name='Sede B')
        make = CustomUser.objects.create_user
        make(username='admin_a', email='admin@a.com', password='x', role=CustomUser.Role.ADMIN,
             institution=self.institution)
        make(username='admin_off', email='off@a.com', password='x', role=CustomUser.Role.ADMIN,
             institution=self.institution, is_active=False)
        make(username='admin_b', email='admin@b.com', password='x', role=CustomUser.Role.ADMIN, institution=other)
        make(username='staff_a', email='staff@a.com', password='x', role=CustomUser.Role.OPERATIONS_STAFF,
             institution=self.institution)
        self.low = MenuItem.objects.create(institution=self.institution, name='Sanduche', price=1, stock=3)
        self.out = MenuItem.objects.create(institution=self.institution, name='Cola', price=1, stock=0)

    def test_only_the_active_admins_of_the_institution_get_it(self):
        send_low_stock_email(self.institution.id, [self.low.id, self.out.id], [])

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ['admin@a.com'])
        self.assertIn('Stock bajo', message.subject)
        self.assertIn('Quedan 3', message.body)
        self.assertIn('Agotado', message.body)

    def test_nothing_is_sent_without_admins(self):
        send_low_stock_email(Institution.objects.create(name='Sede C').id, [self.low.id], [])
        self.assertEqual(mail.outbox, [])
