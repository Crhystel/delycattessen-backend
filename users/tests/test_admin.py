from unittest.mock import patch

from django.conf import settings
from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.test import RequestFactory, TestCase
from django.urls import reverse
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
from rest_framework_simplejwt.tokens import RefreshToken

from users.models import CustomUser, Institution, ParentProfile, StudentProfile
from wallet.models import Wallet

Role = CustomUser.Role
PASSWORD = 'test1234'


class SuperadminTestCase(TestCase):
    def setUp(self):
        self.institution = Institution.objects.create(name='Martim Cereré')
        self.root = CustomUser.objects.create_superuser('root', 'root@delycattessen.com', PASSWORD)
        self.client.force_login(self.root)

    def make_staff(self, email, role=Role.OPERATIONS_STAFF, pending=False):
        return CustomUser.objects.create_user(
            username=email, email=email, password=PASSWORD, first_name='ANA', last_name='PEREZ',
            role=role, institution=self.institution, must_change_password=pending,
        )

    def make_family(self):
        parent_user = CustomUser.objects.create_user(
            username='padre', email='padre@x.com', password=PASSWORD, role=Role.PARENT,
        )
        parent = ParentProfile.objects.create(user=parent_user)
        student_user = CustomUser.objects.create_user(
            username='hijo', email='hijo@x.com', password=PASSWORD, role=Role.STUDENT,
        )
        student = StudentProfile.objects.create(user=student_user, institution=self.institution, parent=parent)
        Wallet.objects.create(student=student, low_balance_threshold=2)
        return parent_user, student_user

    def run_action(self, name, users):
        return self.client.post(
            reverse('admin:users_customuser_changelist'),
            {'action': name, '_selected_action': [u.pk for u in users], 'index': 0},
            follow=True,
        )


class SuperadminSiteTests(SuperadminTestCase):
    def test_createsuperuser_gets_the_admin_role(self):
        self.assertEqual(self.root.role, Role.ADMIN)

    def test_index_groups_models_by_function_in_spanish(self):
        response = self.client.get(reverse('admin:index'))
        self.assertEqual(response.status_code, 200)
        titles = [section['name'] for section in response.context['app_list']]
        self.assertEqual(titles, ['Personas y cuentas', 'Seguridad y sistema'])

    def test_internal_bookkeeping_models_are_hidden(self):
        response = self.client.get(reverse('admin:index'))
        shown = {m['object_name'] for section in response.context['app_list'] for m in section['models']}
        for hidden in ('Group', 'TaskResult', 'GroupResult', 'OutstandingToken', 'BlacklistedToken',
                       'AccessFailureLog'):
            self.assertNotIn(hidden, shown)
        # The audit trail and the login history are shown instead, and cannot be edited.
        for visible in ('AuditLog', 'LogEntry', 'AccessLog', 'AccessAttempt'):
            self.assertIn(visible, shown)

    def test_no_registered_model_can_be_deleted(self):
        request = RequestFactory().get('/')
        request.user = self.root
        for model, model_admin in admin.site._registry.items():
            if model._meta.app_label == 'axes':
                continue  # deleting a failed-login record is how a blocked user is unlocked
            self.assertFalse(model_admin.has_delete_permission(request), model.__name__)

    def test_bulk_delete_action_is_disabled(self):
        response = self.client.get(reverse('admin:users_customuser_changelist'))
        actions = dict(response.context['action_form'].fields['action'].choices)
        self.assertNotIn('delete_selected', actions)

    def test_delete_page_of_an_account_is_forbidden(self):
        user = self.make_staff('cajero@x.com')
        response = self.client.get(reverse('admin:users_customuser_delete', args=[user.pk]))
        self.assertEqual(response.status_code, 403)

    def test_only_people_management_tables_are_registered(self):
        registered = {model._meta.label_lower for model in admin.site._registry}
        for gone in ('wallet.wallet', 'wallet.transaction', 'pos.sale', 'pos.preorder', 'pos.creditstatement',
                     'pos.reportexport', 'catalog.allergen', 'catalog.ingredient', 'catalog.menuitem',
                     'catalog.menudish', 'catalog.monthlymenu', 'catalog.combo', 'users.parentalcontrol',
                     'users.userbiometric'):
            self.assertNotIn(gone, registered)
        for kept in ('users.customuser', 'users.parentprofile', 'users.studentprofile', 'users.institution',
                     'users.auditlog'):
            self.assertIn(kept, registered)

    def test_profiles_are_read_only_and_hide_sensitive_data(self):
        parent_user, student_user = self.make_family()
        self.assertEqual(self.client.get(reverse('admin:users_studentprofile_add')).status_code, 403)
        parent = parent_user.parent_profile
        response = self.client.get(reverse('admin:users_parentprofile_change', args=[parent.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'payment_pin_hash')
        student = student_user.student_profile
        response = self.client.get(reverse('admin:users_studentprofile_change', args=[student.pk]))
        self.assertNotContains(response, 'profile_picture')

    def test_the_action_log_is_visible_but_not_editable(self):
        LogEntry.objects.log_actions(
            self.root.pk, [self.root], 2, change_message='prueba',
        )
        self.assertEqual(self.client.get(reverse('admin:admin_logentry_changelist')).status_code, 200)
        self.assertEqual(self.client.get(reverse('admin:admin_logentry_add')).status_code, 403)


class LoginProtectionTests(TestCase):
    def setUp(self):
        self.institution = Institution.objects.create(name='Martim Cereré')
        self.root = CustomUser.objects.create_superuser('root', 'root@delycattessen.com', PASSWORD)

    def test_admin_login_is_locked_after_repeated_failures(self):
        url = reverse('admin:login')
        for _attempt in range(settings.AXES_FAILURE_LIMIT):
            self.client.post(url, {'username': 'root', 'password': 'incorrecta'})
        response = self.client.post(url, {'username': 'root', 'password': PASSWORD})
        self.assertIn(response.status_code, (403, 429))
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_the_api_login_is_not_affected_by_the_admin_lockout(self):
        staff = CustomUser.objects.create_user(
            username='cajero@x.com', email='cajero@x.com', password=PASSWORD,
            role=Role.OPERATIONS_STAFF, institution=self.institution,
        )
        for _attempt in range(settings.AXES_FAILURE_LIMIT + 2):
            self.client.post(
                '/api/users/login/', {'username': staff.email, 'email': staff.email, 'password': 'incorrecta'},
                content_type='application/json',
            )
        response = self.client.post(
            '/api/users/login/', {'username': staff.email, 'email': staff.email, 'password': PASSWORD},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)


class AccountCreationTests(SuperadminTestCase):
    url = property(lambda self: reverse('admin:users_customuser_add'))

    def payload(self, **overrides):
        data = {
            'email': 'Nuevo.Docente@X.com', 'first_name': 'luis', 'second_name': '', 'last_name': 'mora',
            'second_last_name': '', 'role': Role.TEACHER, 'institution': self.institution.pk,
        }
        data.update(overrides)
        return data

    def test_staff_account_is_created_through_the_api_service(self):
        with patch('users.admin.notify_staff_credentials', return_value=True) as notify:
            response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 302, getattr(response, 'context', None) and response.context['adminform'].form.errors)
        user = CustomUser.objects.get(email='nuevo.docente@x.com')
        self.assertEqual(user.role, Role.TEACHER)
        self.assertEqual(user.username, 'nuevo.docente@x.com')
        self.assertEqual((user.first_name, user.last_name), ('LUIS', 'MORA'))
        self.assertTrue(user.must_change_password)
        self.assertTrue(user.has_usable_password())
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], user)

    def test_parents_and_students_cannot_be_created_here(self):
        for role in (Role.PARENT, Role.STUDENT):
            response = self.client.post(self.url, self.payload(role=role))
            self.assertEqual(response.status_code, 200)
        self.assertFalse(CustomUser.objects.filter(email='nuevo.docente@x.com').exists())

    def test_email_must_be_unique_ignoring_case(self):
        self.make_staff('nuevo.docente@x.com')
        response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(CustomUser.objects.filter(email__iexact='nuevo.docente@x.com').count(), 1)

    def test_every_staff_role_needs_a_sede(self):
        response = self.client.post(self.url, self.payload(institution=''))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CustomUser.objects.filter(email='nuevo.docente@x.com').exists())


class AccountEditingTests(SuperadminTestCase):
    def change_url(self, user):
        return reverse('admin:users_customuser_change', args=[user.pk])

    def test_username_is_not_editable_and_role_stays_within_staff_roles(self):
        user = self.make_staff('cajero@x.com')
        self.client.post(self.change_url(user), {
            'email': 'cajero@x.com', 'first_name': 'ANA', 'last_name': 'PEREZ', 'institution': self.institution.pk,
            'is_active': 'on', 'role': Role.STUDENT, 'username': 'otro',
        })
        user.refresh_from_db()
        self.assertEqual(user.role, Role.OPERATIONS_STAFF)  # STUDENT is not an allowed choice
        self.assertEqual(user.username, 'cajero@x.com')

    def test_changing_the_email_keeps_username_in_sync_and_lowercases(self):
        user = self.make_staff('cajero@x.com')
        response = self.client.post(self.change_url(user), {
            'email': 'Nuevo@X.com', 'first_name': 'ana', 'last_name': 'perez', 'institution': self.institution.pk,
            'is_active': 'on', 'role': Role.OPERATIONS_STAFF,
        })
        self.assertEqual(response.status_code, 302)
        user.refresh_from_db()
        self.assertEqual((user.email, user.username, user.first_name), ('nuevo@x.com', 'nuevo@x.com', 'ANA'))

    def test_a_soft_deleted_account_is_read_only(self):
        from users.services import soft_delete_account
        user = soft_delete_account(self.make_staff('cajero@x.com'))
        self.client.post(self.change_url(user), {'first_name': 'HACKED', 'is_active': 'on'})
        user.refresh_from_db()  # no field is editable, so nothing was saved
        self.assertEqual(user.first_name, 'ANA')
        self.assertFalse(user.is_active)

    def test_own_privileges_cannot_be_edited(self):
        readonly = admin.site._registry[CustomUser].get_readonly_fields(
            self._request(), self.root,
        )
        self.assertIn('is_superuser', readonly)
        self.assertIn('is_active', readonly)

    def _request(self):
        request = RequestFactory().get('/')
        request.user = self.root
        return request

    def test_password_cannot_be_set_by_hand(self):
        user = self.make_staff('cajero@x.com')
        old_hash = user.password
        self.client.post(
            f'/admin/users/customuser/{user.pk}/password/', {'password1': 'Nueva1234!', 'password2': 'Nueva1234!'},
        )
        self.client.post(self.change_url(user), {
            'email': user.email, 'first_name': 'ANA', 'last_name': 'PEREZ', 'institution': self.institution.pk,
            'is_active': 'on', 'password': 'Nueva1234!',
        })
        user.refresh_from_db()
        self.assertEqual(user.password, old_hash)  # only the "reset temporary password" action changes it

    def test_inlines_depend_on_the_role(self):
        parent_user, student_user = self.make_family()
        staff = self.make_staff('cajero@x.com')
        site_admin = admin.site._registry[CustomUser]
        request = self._request()
        counts = {
            user.role: len(site_admin.get_inline_instances(request, user))
            for user in (parent_user, student_user, staff)
        }
        self.assertEqual(counts, {Role.PARENT: 1, Role.STUDENT: 1, Role.OPERATIONS_STAFF: 0})

    def test_filter_finds_students_without_a_profile(self):
        orphan = CustomUser.objects.create_user(
            username='huerfano', email='huerfano@x.com', password=PASSWORD, role=Role.STUDENT,
        )
        _parent, student_user = self.make_family()
        response = self.client.get(reverse('admin:users_customuser_changelist'), {'sin_perfil': 'estudiante'})
        usernames = {u.username for u in response.context['cl'].result_list}
        self.assertEqual(usernames, {orphan.username})
        self.assertNotIn(student_user.username, usernames)


class AccountActionTests(SuperadminTestCase):
    def test_activate_and_deactivate_revoke_sessions(self):
        user = self.make_staff('cajero@x.com')
        RefreshToken.for_user(user)
        self.run_action('deactivate_accounts', [user])
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertEqual(BlacklistedToken.objects.filter(token__user=user).count(), 1)
        self.run_action('activate_accounts', [user])
        user.refresh_from_db()
        self.assertTrue(user.is_active)

    def test_own_account_and_superusers_are_never_deactivated(self):
        other_root = CustomUser.objects.create_superuser('root2', 'root2@x.com', PASSWORD)
        self.run_action('deactivate_accounts', [self.root, other_root])
        self.root.refresh_from_db()
        other_root.refresh_from_db()
        self.assertTrue(self.root.is_active and other_root.is_active)

    def test_soft_delete_follows_the_staff_policy(self):
        operative = self.make_staff('cajero@x.com')
        pending = self.make_staff('nuevo@x.com', Role.TEACHER, pending=True)
        active_teacher = self.make_staff('docente@x.com', Role.TEACHER)
        parent_user, _student = self.make_family()
        self.run_action('soft_delete_staff', [operative, pending, active_teacher, parent_user])

        for gone in (operative, pending):
            gone.refresh_from_db()
            self.assertIsNotNone(gone.deleted_at)
            self.assertFalse(gone.is_active)
            self.assertIsNone(gone.email)
            self.assertEqual(gone.username, f'deleted-{gone.pk}')
        for kept in (active_teacher, parent_user):
            kept.refresh_from_db()
            self.assertIsNone(kept.deleted_at)
            self.assertTrue(kept.is_active)

    def test_reset_temporary_password_mails_a_new_one_and_closes_sessions(self):
        operative = self.make_staff('cajero@x.com')
        old_hash = operative.password
        RefreshToken.for_user(operative)
        with patch('users.admin_services.notify_password_reset', return_value=True) as notify:
            self.run_action('reset_temporary_password', [operative])
        operative.refresh_from_db()
        self.assertNotEqual(operative.password, old_hash)
        self.assertTrue(operative.must_change_password)
        self.assertEqual(BlacklistedToken.objects.filter(token__user=operative).count(), 1)
        notify.assert_called_once()
        recipient, user, login, temporary = notify.call_args.args
        self.assertEqual((recipient, login, user), (operative.email, operative.email, operative))
        self.assertTrue(operative.check_password(temporary))
