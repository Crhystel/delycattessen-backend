from unittest import mock

from django.core import mail
from django.test import SimpleTestCase
from kombu.exceptions import OperationalError
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from users.models import CustomUser, Institution
from users.services import TEMPORARY_PASSWORD_LENGTH, generate_temporary_password
from users.tasks import send_staff_credentials_email

STAFF_URL = '/api/users/staff/'
DELAY = 'users.tasks.send_staff_credentials_email.delay'


class StaffCredentialsEmailTests(APITestCase):
    """The temporary password of a new teacher / operative reaches the user by
    email; the API never returns it to the admin."""

    def setUp(self):
        self.institution = Institution.objects.create(name='Martim Cereré')
        self.admin = CustomUser.objects.create_user(
            username='admin', email='admin@delycattessen.com', password='test1234',
            role=CustomUser.Role.ADMIN, institution=self.institution,
        )
        token = RefreshToken.for_user(self.admin)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.access_token}')

    def _create(self, email='nuevo@delycattessen.com', role=CustomUser.Role.TEACHER):
        return self.client.post(STAFF_URL, {
            'email': email, 'first_name': 'Ana', 'last_name': 'Perez', 'role': role,
        }, format='json')

    def test_response_does_not_expose_the_temporary_password(self):
        with mock.patch(DELAY):
            response = self._create()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertNotIn('temporary_password', response.data)
        self.assertTrue(response.data['email_sent'])

    def test_email_is_enqueued_for_teacher_and_operative(self):
        for role, label in (
            (CustomUser.Role.TEACHER, 'Docente'),
            (CustomUser.Role.OPERATIONS_STAFF, 'Personal Operativo'),
        ):
            email = f'{role.lower()}@delycattessen.com'
            with mock.patch(DELAY) as delay:
                self.assertEqual(self._create(email, role).status_code, 201)
            args = delay.call_args.args
            self.assertEqual(args[0], email)
            self.assertEqual(args[1], 'ANA')
            self.assertEqual(args[2], label)

    def test_enqueued_password_is_the_one_that_logs_in_and_must_be_changed(self):
        with mock.patch(DELAY) as delay:
            self._create()
        password = delay.call_args.args[3]
        user = CustomUser.objects.get(email='nuevo@delycattessen.com')
        self.assertTrue(user.check_password(password))
        self.assertTrue(user.must_change_password)

    def test_unreachable_broker_still_creates_the_account(self):
        with mock.patch(DELAY, side_effect=OperationalError('down')):
            response = self._create()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(response.data['email_sent'])
        self.assertTrue(CustomUser.objects.filter(email='nuevo@delycattessen.com').exists())

    def test_task_sends_a_branded_email_with_the_password(self):
        mail.outbox.clear()
        send_staff_credentials_email('nuevo@delycattessen.com', 'ANA', 'Docente', 'Kx7mQ4pw')

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ['nuevo@delycattessen.com'])
        self.assertIn('Usuario: nuevo@delycattessen.com', message.body)
        self.assertIn('Contraseña temporal: Kx7mQ4pw', message.body)
        html, mimetype = message.alternatives[0]
        self.assertEqual(mimetype, 'text/html')
        self.assertIn('Kx7mQ4pw', html)
        self.assertIn('Docente', html)
        self.assertIn("D'ELYCATTESSEN", html)  # shared layout (emails/base.html)

    def test_email_highlights_user_and_password_in_their_own_box(self):
        mail.outbox.clear()
        send_staff_credentials_email('nuevo@delycattessen.com', 'ANA', 'Docente', 'Kx7mQ4pw')

        html = mail.outbox[0].alternatives[0][0]
        self.assertEqual(html.count('#fef8ec'), 2)  # one yellow box per credential
        self.assertIn('Usuario', html)
        self.assertIn('Contraseña temporal', html)
        self.assertIn('nuevo@delycattessen.com', html)


class GenerateTemporaryPasswordTests(SimpleTestCase):
    """The temporary password is short and cannot be misread or split when copied."""

    def test_is_short_and_easy_to_read_and_copy(self):
        for _unused in range(200):
            password = generate_temporary_password()
            self.assertEqual(len(password), TEMPORARY_PASSWORD_LENGTH)
            self.assertTrue(password.isalnum(), password)  # no symbols: a double click selects it all
            self.assertFalse(set(password) & set('ILOilo01'), password)  # no look-alikes
            self.assertTrue(any(c.isupper() for c in password), password)
            self.assertTrue(any(c.islower() for c in password), password)
            self.assertTrue(any(c.isdigit() for c in password), password)
