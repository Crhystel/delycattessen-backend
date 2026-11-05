from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken

from users.mixins import reset_code_key
from users.models import CustomUser

LOCMEM_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
CHANGE_URL = '/api/users/change-password/'
REFRESH_URL = '/api/users/login/refresh/'
CONFIRM_URL = '/api/users/password-reset/confirm/'
OLD_PASSWORD = 'Old-Passw0rd!'
NEW_PASSWORD = 'New-Passw0rd!'


@override_settings(CACHES=LOCMEM_CACHE)
class ChangePasswordTests(APITestCase):
    """The authenticated change keeps the project password policy, limits the
    attempts and closes the sessions opened on other devices."""

    def setUp(self):
        cache.clear()
        self.user = CustomUser.objects.create_user(
            username='docente', email='docente@x.com', password=OLD_PASSWORD, role=CustomUser.Role.TEACHER,
        )
        self.other_device = RefreshToken.for_user(self.user)
        self.this_device = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.this_device.access_token}')

    def _change(self, current=OLD_PASSWORD, new=NEW_PASSWORD, confirm=None):
        return self.client.post(CHANGE_URL, {
            'current_password': current,
            'new_password': new,
            'confirm_password': new if confirm is None else confirm,
        }, format='json')

    def _refresh(self, token):
        return self.client.post(REFRESH_URL, {'refresh': str(token)}, format='json')

    def test_requires_authentication(self):
        self.client.credentials()
        self.assertEqual(self._change().status_code, 401)

    def test_wrong_current_password_is_rejected(self):
        response = self._change(current='Wrong-Passw0rd!')
        self.assertEqual(response.status_code, 400)
        self.assertIn('current_password', response.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(OLD_PASSWORD))

    def test_weak_new_password_is_rejected(self):
        self.assertEqual(self._change(new='abc').status_code, 400)

    def test_confirmation_must_match(self):
        self.assertEqual(self._change(confirm='Other-Passw0rd!').status_code, 400)

    def test_new_password_must_differ_from_the_current_one(self):
        self.assertEqual(self._change(new=OLD_PASSWORD).status_code, 400)

    def test_success_changes_the_password_and_returns_new_tokens(self):
        self.user.must_change_password = True
        self.user.save(update_fields=['must_change_password'])

        response = self._change()

        self.assertEqual(response.status_code, 200)
        self.assertIn('access', response.data)
        self.assertIn('refresh', response.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
        self.assertFalse(self.user.must_change_password)

    def test_success_closes_other_sessions_but_keeps_this_device(self):
        response = self._change()

        self.assertEqual(self._refresh(self.other_device).status_code, 401)
        self.assertEqual(self._refresh(self.this_device).status_code, 401)
        self.assertEqual(self._refresh(response.data['refresh']).status_code, 200)

    def test_failed_change_keeps_every_session(self):
        self._change(current='Wrong-Passw0rd!')

        self.assertEqual(self._refresh(self.other_device).status_code, 200)

    def test_attempts_are_limited(self):
        with mock.patch.object(ScopedRateThrottle, 'THROTTLE_RATES', {'change_password': '3/hour'}):
            statuses = [self._change(current='Wrong-Passw0rd!').status_code for _unused in range(4)]
            blocked_valid = self._change().status_code

        self.assertEqual(statuses, [400, 400, 400, 429])
        self.assertEqual(blocked_valid, 429)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(OLD_PASSWORD))


@override_settings(CACHES=LOCMEM_CACHE)
class PasswordResetClosesSessionsTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = CustomUser.objects.create_user(
            username='reset', email='reset@x.com', password=OLD_PASSWORD, role=CustomUser.Role.PARENT,
        )

    def test_confirming_a_reset_revokes_the_refresh_tokens(self):
        token = RefreshToken.for_user(self.user)
        cache.set(reset_code_key('reset@x.com'), '123456', timeout=60)

        response = self.client.post(CONFIRM_URL, {
            'email': 'reset@x.com', 'code': '123456',
            'new_password': NEW_PASSWORD, 'confirm_password': NEW_PASSWORD,
        }, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('refresh', response.data)
        refreshed = self.client.post(REFRESH_URL, {'refresh': str(token)}, format='json')
        self.assertEqual(refreshed.status_code, 401)
