from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from users.mixins import MAX_CODE_ATTEMPTS, reset_code_key
from users.models import CustomUser

LOCMEM_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
REQUEST_URL = '/api/users/password-reset/request/'
CONFIRM_URL = '/api/users/password-reset/confirm/'
NEW_PASSWORD = 'Str0ng-Passw0rd!'


@override_settings(CACHES=LOCMEM_CACHE)
class PasswordResetTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = CustomUser.objects.create_user(
            username='reset', email='reset@x.com', password='Old-Passw0rd!', role=CustomUser.Role.PARENT,
        )

    def _request_code(self):
        with mock.patch('users.tasks.send_password_reset_email.delay') as send:
            response = self.client.post(REQUEST_URL, {'email': 'reset@x.com'}, format='json')
        return response, send

    def _confirm(self, code, password=NEW_PASSWORD):
        return self.client.post(CONFIRM_URL, {
            'email': 'reset@x.com', 'code': code, 'new_password': password, 'confirm_password': password,
        }, format='json')

    def test_unknown_email_gets_the_same_answer_and_no_email(self):
        with mock.patch('users.tasks.send_password_reset_email.delay') as send:
            known = self.client.post(REQUEST_URL, {'email': 'reset@x.com'}, format='json')
            unknown = self.client.post(REQUEST_URL, {'email': 'nobody@x.com'}, format='json')
        self.assertEqual(known.status_code, 200)
        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(known.data, unknown.data)
        self.assertEqual(send.call_count, 1)

    def test_valid_code_resets_the_password(self):
        _, send = self._request_code()
        code = send.call_args.args[1]
        self.assertEqual(len(code), 6)
        self.assertEqual(self._confirm(code).status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
        self.assertIsNone(cache.get(reset_code_key('reset@x.com')))

    def test_code_is_locked_after_too_many_wrong_attempts(self):
        _, send = self._request_code()
        code = send.call_args.args[1]
        wrong = '000000' if code != '000000' else '111111'
        for _attempt in range(MAX_CODE_ATTEMPTS):
            self.assertEqual(self._confirm(wrong).status_code, 400)
        # Even the right code is refused once the attempts are used up.
        self.assertIn(self._confirm(code).status_code, (400, 401, 403))
        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password(NEW_PASSWORD))

    def test_requests_are_rate_limited_per_email(self):
        statuses = []
        with mock.patch('users.tasks.send_password_reset_email.delay'):
            for _attempt in range(7):
                statuses.append(
                    self.client.post(REQUEST_URL, {'email': 'reset@x.com'}, format='json').status_code
                )
        self.assertEqual(statuses[:5], [200] * 5)
        self.assertNotIn(200, statuses[5:])
        self.assertEqual(len(statuses[5:]), 2)
