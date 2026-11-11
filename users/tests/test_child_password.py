from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken

from users.models import AuditLog, CustomUser, Institution, ParentProfile, StudentProfile

Role = CustomUser.Role
LOCMEM_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
REFRESH_URL = '/api/users/login/refresh/'
PARENT_PASSWORD = 'Parent-Passw0rd!'
OLD_CHILD_PASSWORD = 'Child-Old-Passw0rd!'
NEW_CHILD_PASSWORD = 'Child-New-Passw0rd!'


def make_family(institution, suffix, parent_email='padre@x.com'):
    parent_user = CustomUser.objects.create_user(
        username=f'padre{suffix}', email=parent_email, password=PARENT_PASSWORD, role=Role.PARENT,
        first_name='ANA', last_name='PEREZ',
    )
    parent = ParentProfile.objects.create(user=parent_user)
    student_user = CustomUser.objects.create_user(
        username=f'hijo{suffix}', email=f'hijo{suffix}@x.com', password=OLD_CHILD_PASSWORD, role=Role.STUDENT,
        first_name='LUIS', last_name='PEREZ', must_change_password=True,
    )
    student = StudentProfile.objects.create(user=student_user, institution=institution, parent=parent)
    return parent_user, student


@override_settings(CACHES=LOCMEM_CACHE)
class ChildPasswordTests(APITestCase):
    """The parent sets their child's password confirming with their own: the child's
    sessions close, the parent keeps theirs and gets an e-mail notice without it."""

    def setUp(self):
        cache.clear()
        self.institution = Institution.objects.create(name='Sede A')
        self.parent_user, self.student = make_family(self.institution, 'a')
        self.child = self.student.user
        self.child_session = RefreshToken.for_user(self.child)
        self.parent_session = RefreshToken.for_user(self.parent_user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.parent_session.access_token}')

    def url(self, student=None):
        return f'/api/users/children/{(student or self.student).pk}/password/'

    def change(self, parent_password=PARENT_PASSWORD, new=NEW_CHILD_PASSWORD, confirm=None, student=None):
        return self.client.post(self.url(student), {
            'parent_password': parent_password,
            'new_password': new,
            'confirm_password': new if confirm is None else confirm,
        }, format='json')

    def refresh(self, token):
        return self.client.post(REFRESH_URL, {'refresh': str(token)}, format='json')

    def test_the_child_gets_the_new_password_and_loses_the_old_one(self):
        response = self.change()
        self.assertEqual(response.status_code, 200)
        self.child.refresh_from_db()
        self.assertTrue(self.child.check_password(NEW_CHILD_PASSWORD))
        self.assertFalse(self.child.check_password(OLD_CHILD_PASSWORD))
        self.assertFalse(self.child.must_change_password)

    def test_the_child_sessions_close_but_the_parent_keeps_theirs(self):
        self.change()
        self.assertEqual(self.refresh(self.child_session).status_code, 401)
        self.assertEqual(self.refresh(self.parent_session).status_code, 200)

    def test_the_parent_is_notified_without_the_password(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.change()
        args = self.mock_delay_args()
        self.assertEqual(args[0], 'padre@x.com')
        self.assertEqual(args[3], self.child.username)
        self.assertNotIn(NEW_CHILD_PASSWORD, ' '.join(str(a) for a in args))

    def mock_delay_args(self):
        from users import tasks
        return tasks.send_child_password_changed_email.delay.call_args.args

    def test_a_parent_without_email_still_changes_the_password(self):
        self.parent_user.email = None
        self.parent_user.save(update_fields=['email'])
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.change().status_code, 200)
        from users import tasks
        tasks.send_child_password_changed_email.delay.assert_not_called()

    def test_a_wrong_parent_password_is_a_400_not_a_401(self):
        response = self.change(parent_password='Wrong-Passw0rd!')
        self.assertEqual(response.status_code, 400)
        self.assertIn('parent_password', response.data)
        self.child.refresh_from_db()
        self.assertTrue(self.child.check_password(OLD_CHILD_PASSWORD))

    def test_a_missing_parent_password_is_rejected(self):
        response = self.client.post(self.url(), {
            'new_password': NEW_CHILD_PASSWORD, 'confirm_password': NEW_CHILD_PASSWORD,
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_a_weak_password_is_rejected(self):
        self.assertEqual(self.change(new='abc').status_code, 400)

    def test_the_confirmation_must_match(self):
        self.assertEqual(self.change(confirm='Other-Passw0rd!').status_code, 400)

    def test_another_parents_child_is_forbidden(self):
        _other_parent, other_student = make_family(self.institution, 'b', parent_email='otro@x.com')
        self.assertEqual(self.change(student=other_student).status_code, 403)
        other_student.user.refresh_from_db()
        self.assertTrue(other_student.user.check_password(OLD_CHILD_PASSWORD))

    def test_a_non_parent_is_forbidden(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.child).access_token}')
        self.assertEqual(self.change().status_code, 403)

    def test_requires_authentication(self):
        self.client.credentials()
        self.assertEqual(self.change().status_code, 401)

    def test_the_change_is_audited_without_secrets(self):
        self.change()
        entry = AuditLog.objects.get(action=AuditLog.Action.PASSWORD_CHANGED)
        self.assertEqual(entry.actor, self.parent_user)
        self.assertEqual(entry.target_id, self.child.pk)
        self.assertEqual(entry.source, AuditLog.Source.API)
        self.assertNotIn(NEW_CHILD_PASSWORD, str(entry.detail))

    def test_attempts_are_rate_limited(self):
        with mock.patch.object(ScopedRateThrottle, 'THROTTLE_RATES', {'child_account': '2/hour'}):
            self.assertEqual(self.change(parent_password='Wrong-Passw0rd!').status_code, 400)
            self.assertEqual(self.change(parent_password='Wrong-Passw0rd!').status_code, 400)
            self.assertEqual(self.change().status_code, 429)
