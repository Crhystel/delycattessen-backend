import json
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from celery.exceptions import Retry
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from kombu.exceptions import OperationalError
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from catalog.models import Combo, ComboItem, Ingredient, MenuItem
from catalog.services import ComboService
from catalog.tasks import format_validity, send_combo_created_email
from users.models import CustomUser, Institution, ParentProfile, StudentProfile

COMBOS = '/api/catalog/combos/'
TODAY = date(2026, 11, 12)
LOCMEM = 'django.core.mail.backends.locmem.EmailBackend'
GIF = (
    b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04'
    b'\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
)


def _image(name='combo.gif'):
    return SimpleUploadedFile(name, GIF, content_type='image/gif')


class FormatValidityTests(SimpleTestCase):
    def test_same_month(self):
        self.assertEqual(
            format_validity(date(2026, 11, 12), date(2026, 11, 18), TODAY),
            'Válido del 12 al 18 de noviembre de 2026',
        )

    def test_different_months(self):
        self.assertEqual(
            format_validity(date(2026, 11, 28), date(2026, 12, 4), date(2026, 11, 29)),
            'Válido del 28 de noviembre al 4 de diciembre de 2026',
        )

    def test_different_years(self):
        self.assertEqual(
            format_validity(date(2026, 12, 28), date(2027, 1, 4), date(2026, 12, 30)),
            'Válido del 28 de diciembre de 2026 al 4 de enero de 2027',
        )

    def test_single_day(self):
        self.assertEqual(format_validity(TODAY, TODAY, TODAY), 'Válido el 12 de noviembre de 2026')

    def test_starting_in_the_future(self):
        self.assertEqual(
            format_validity(date(2026, 11, 20), date(2026, 11, 27), TODAY),
            'Disponible desde el 20 de noviembre hasta el 27 de noviembre de 2026',
        )
        self.assertEqual(
            format_validity(date(2026, 11, 20), date(2026, 11, 20), TODAY),
            'Disponible el 20 de noviembre de 2026',
        )

    def test_starting_today_is_already_valid(self):
        self.assertTrue(format_validity(TODAY, TODAY + timedelta(days=3), TODAY).startswith('Válido del'))


@mock.patch('catalog.tasks.local_today', return_value=TODAY)
@mock.patch.object(ComboService, 'today', return_value=TODAY)
class ComboEmailTests(APITestCase):
    """The new-combo notification: trigger on creation and the email task."""

    def setUp(self):
        make = CustomUser.objects.create_user
        self.inst_a = Institution.objects.create(name='Sede A')
        self.inst_b = Institution.objects.create(name='Sede B')
        self.admin_a = make(username='admin_a', email='aa@x.com', password='x',
                            role=CustomUser.Role.ADMIN, institution=self.inst_a)

        ingredient = Ingredient.objects.create(name='Base combo', allergens_reviewed=True)
        self.crepes = self._product('Crepes', '2.50', ingredient)
        self.tostada = self._product('Tostada', '1.50', ingredient)

        self.parent_a = self._parent('parent_a', self.inst_a, first_name='Carlos', last_name='Perez')
        self.parent_b = self._parent('parent_b', self.inst_b)

    def _product(self, name, price, ingredient):
        item = MenuItem.objects.create(institution=self.inst_a, name=name, price=Decimal(price), stock=10)
        item.ingredients.add(ingredient)
        return item

    def _parent(self, username, institution, email=True, active=True, **extra):
        user = CustomUser.objects.create_user(
            username=username, email=f'{username}@gmail.com' if email else '', password='x',
            role=CustomUser.Role.PARENT, is_active=active, **extra,
        )
        profile = ParentProfile.objects.create(user=user)
        child = CustomUser.objects.create_user(
            username=f'child_{username}', email=f'child_{username}@gmail.com', password='x',
            role=CustomUser.Role.STUDENT,
        )
        StudentProfile.objects.create(user=child, parent=profile, institution=institution)
        return user

    def _combo(self, items=None, price='3.50', start=TODAY, days=6, name='Combo desayuno'):
        combo = Combo.objects.create(
            institution=self.inst_a, name=name, price=Decimal(price),
            start_date=start, end_date=start + timedelta(days=days), image=_image(),
        )
        for product, quantity in items if items is not None else [(self.crepes, 1), (self.tostada, 1)]:
            ComboItem.objects.create(combo=combo, menu_item=product, quantity=quantity)
        return combo

    def _auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _payload(self):
        return {
            'name': 'Combo desayuno',
            'price': '3.50',
            'start_date': TODAY.isoformat(),
            'end_date': (TODAY + timedelta(days=6)).isoformat(),
            'items': json.dumps([{'product_id': self.crepes.id, 'quantity': 1}]),
            'image': _image(),
        }

    @staticmethod
    def _html(message):
        return message.alternatives[0].content

    @staticmethod
    def _inline_images(message):
        return [part for part in message.message().walk() if part.get('Content-ID')]

    # ---- trigger ------------------------------------------------------------
    def test_creating_a_combo_enqueues_the_email_task(self, _today, _local_today):
        self._auth(self.admin_a)
        with mock.patch('catalog.services.send_combo_created_email.delay') as delay, \
                self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(COMBOS, self._payload(), format='multipart')
        self.assertEqual(response.status_code, 201, response.data)
        delay.assert_called_once_with(Combo.objects.get().id)

    def test_editing_and_deleting_do_not_enqueue_the_task(self, _today, _local_today):
        combo = self._combo()
        self._auth(self.admin_a)
        with mock.patch('catalog.services.send_combo_created_email.delay') as delay, \
                self.captureOnCommitCallbacks(execute=True):
            patched = self.client.patch(f'{COMBOS}{combo.id}/', {'price': '3.00'}, format='multipart')
            deleted = self.client.delete(f'{COMBOS}{combo.id}/')
        self.assertEqual((patched.status_code, deleted.status_code), (200, 204))
        delay.assert_not_called()

    def test_unreachable_broker_does_not_break_the_creation(self, _today, _local_today):
        self._auth(self.admin_a)
        with mock.patch('catalog.services.send_combo_created_email.delay', side_effect=OperationalError('down')), \
                self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(COMBOS, self._payload(), format='multipart')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(Combo.objects.exists())

    # ---- recipients ---------------------------------------------------------
    @override_settings(EMAIL_BACKEND=LOCMEM)
    def test_only_active_parents_with_email_and_children_in_the_institution_receive_it(self, _today, _local_today):
        self._parent('parent_inactive', self.inst_a, active=False)
        self._parent('parent_no_email', self.inst_a, email=False)
        second_child = CustomUser.objects.create_user(
            username='second_child', email='second_child@gmail.com', password='x', role=CustomUser.Role.STUDENT,
        )
        StudentProfile.objects.create(user=second_child, parent=self.parent_a.parent_profile, institution=self.inst_a)
        combo = self._combo()

        send_combo_created_email(combo.id)

        self.assertEqual([m.to for m in mail.outbox], [[self.parent_a.email]])

    @override_settings(EMAIL_BACKEND=LOCMEM)
    def test_missing_combo_sends_nothing(self, _today, _local_today):
        send_combo_created_email(999999)
        self.assertEqual(mail.outbox, [])

    # ---- content ------------------------------------------------------------
    @override_settings(EMAIL_BACKEND=LOCMEM)
    def test_email_shows_name_products_validity_and_inline_image(self, _today, _local_today):
        combo = self._combo(items=[(self.crepes, 1), (self.tostada, 2)], name='Combo & desayuno')

        send_combo_created_email(combo.id)

        message = mail.outbox[0]
        self.assertEqual(message.subject, 'Nuevo combo disponible en Snack-Up: Combo & desayuno')
        html = self._html(message)
        for text in ('Estimado/a <strong>Carlos Perez</strong>', "D'Elycattessen está ofreciendo un nuevo combo",
                     '1&times; Crepes', '2&times; Tostada', 'Válido del 12 al 18 de noviembre de 2026',
                     'Nuevo combo disponible', 'cid:combo-image', 'Imagen del combo Combo &amp; desayuno'):
            self.assertIn(text, html)
        for text in ('Combo & desayuno', "D'Elycattessen está ofreciendo un nuevo combo", '1× Crepes',
                     '2× Tostada', 'Válido del 12 al 18 de noviembre de 2026'):
            self.assertIn(text, message.body)
        self.assertNotIn('Sede A', html)
        self.assertNotIn('Sede A', message.body)
        self.assertNotIn('<', message.body)

        mime = message.message()
        self.assertEqual(mime.get_content_type(), 'multipart/related')
        images = self._inline_images(message)
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]['Content-ID'], '<combo-image>')
        self.assertEqual(images[0].get_content_disposition(), 'inline')
        with combo.image.open('rb') as stored:
            self.assertEqual(images[0].get_content(), stored.read())
        self.assertEqual(images[0].get_content_type(), 'image/jpeg')

    @override_settings(EMAIL_BACKEND=LOCMEM)
    def test_each_parent_gets_their_own_inline_image(self, _today, _local_today):
        self._parent('parent_a2', self.inst_a)
        combo = self._combo()

        send_combo_created_email(combo.id)

        self.assertEqual(len(mail.outbox), 2)
        for message in mail.outbox:
            self.assertEqual(len(self._inline_images(message)), 1)
        self.assertIsNot(mail.outbox[0].attachments[0], mail.outbox[1].attachments[0])

    @override_settings(EMAIL_BACKEND=LOCMEM)
    def test_email_does_not_show_any_price(self, _today, _local_today):
        combo = self._combo(items=[(self.crepes, 1), (self.tostada, 2)], price='3.50')

        send_combo_created_email(combo.id)

        message = mail.outbox[0]
        for text in (self._html(message), message.body):
            for forbidden in ('$', 'Ahorras', 'Por separado', '3.50'):
                self.assertNotIn(forbidden, text)

    @override_settings(EMAIL_BACKEND=LOCMEM)
    def test_scheduled_combo_says_when_it_becomes_available(self, _today, _local_today):
        combo = self._combo(start=TODAY + timedelta(days=8), days=7)

        send_combo_created_email(combo.id)

        self.assertIn('Disponible desde el 20 de noviembre hasta el 27 de noviembre de 2026', mail.outbox[0].body)

    @override_settings(EMAIL_BACKEND=LOCMEM)
    def test_unreadable_image_sends_the_email_without_it(self, _today, _local_today):
        combo = self._combo()
        combo.image.storage.delete(combo.image.name)

        with self.assertLogs('catalog.tasks', level='WARNING'):
            send_combo_created_email(combo.id)

        message = mail.outbox[0]
        self.assertEqual(self._inline_images(message), [])
        self.assertEqual(message.message().get_content_type(), 'multipart/alternative')
        html = self._html(message)
        self.assertNotIn('cid:combo-image', html)
        self.assertIn('Combo desayuno', html)

    @override_settings(EMAIL_BACKEND=LOCMEM)
    def test_image_that_is_not_a_picture_sends_the_email_without_it(self, _today, _local_today):
        combo = self._combo()
        with combo.image.storage.open(combo.image.name, 'wb') as image_file:
            image_file.write(b'not an image')

        with self.assertLogs('catalog.tasks', level='WARNING'):
            send_combo_created_email(combo.id)

        self.assertEqual(self._inline_images(mail.outbox[0]), [])

    # ---- delivery -----------------------------------------------------------
    def test_emails_go_out_in_batches_of_50_and_failures_are_retried(self, _today, _local_today):
        for i in range(54):
            self._parent(f'parent_batch_{i}', self.inst_a)
        combo = self._combo()

        connection = mock.MagicMock()
        with mock.patch('catalog.tasks.get_connection', return_value=connection):
            send_combo_created_email(combo.id)
        connection.open.assert_called_once()
        sizes = [len(call.args[0]) for call in connection.send_messages.call_args_list]
        self.assertEqual(sizes, [50, 5])
        connection.close.assert_called_once()

        failing = mock.MagicMock()
        failing.send_messages.side_effect = Exception('SMTP Connection Failed')
        with mock.patch('catalog.tasks.get_connection', return_value=failing), \
                mock.patch.object(send_combo_created_email, 'retry', side_effect=Retry) as retry:
            with self.assertRaises(Retry):
                send_combo_created_email(combo.id)
        retry.assert_called_once()
        failing.close.assert_called_once()
