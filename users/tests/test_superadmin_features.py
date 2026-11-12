"""Superadmin features beyond the user list: password resets for every role, role changes,
sedes, the audit log, the system status page and the alerts."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
from rest_framework_simplejwt.tokens import RefreshToken

from core import alerts, health
from users.admin_services import reset_account_password, role_change_error
from users.audit import record_audit
from users.models import AuditLog, CustomUser, Institution, ParentProfile, StudentProfile
from users.tests.test_admin import SuperadminTestCase
from wallet.models import Wallet

Role = CustomUser.Role
PASSWORD = 'test1234'


class PasswordResetForEveryRoleTests(SuperadminTestCase):
    def reset(self, *users):
        return self.run_action('reset_temporary_password', list(users))

    def test_a_parent_gets_the_temporary_password_at_their_own_email(self):
        parent_user, _student = self.make_family()
        old_hash = parent_user.password
        self.reset(parent_user)
        parent_user.refresh_from_db()
        self.assertNotEqual(parent_user.password, old_hash)
        self.assertTrue(parent_user.must_change_password)
        recipient, first_name, label, login, temporary = self.mail_args()
        self.assertEqual((recipient, login), ('padre@x.com', 'padre@x.com'))
        self.assertTrue(parent_user.check_password(temporary))

    def test_a_student_password_goes_to_the_parent_with_the_student_username(self):
        parent_user, student_user = self.make_family()
        RefreshToken.for_user(student_user)
        self.reset(student_user)
        student_user.refresh_from_db()
        self.assertTrue(student_user.must_change_password)
        recipient, _first, _label, login, temporary = self.mail_args()
        self.assertEqual(recipient, parent_user.email)  # the student has no e-mail
        self.assertEqual(login, student_user.username)
        self.assertTrue(student_user.check_password(temporary))
        self.assertEqual(BlacklistedToken.objects.filter(token__user=student_user).count(), 1)

    def test_a_student_without_a_reachable_parent_is_left_untouched(self):
        parent_user, student_user = self.make_family()
        parent_user.email = None
        parent_user.save(update_fields=['email'])
        old_hash = student_user.password
        self.assertEqual(reset_account_password(student_user, actor=self.root), 'no_recipient')
        student_user.refresh_from_db()
        self.assertEqual(student_user.password, old_hash)
        self.assertFalse(student_user.must_change_password)

    def test_every_staff_role_including_another_administrator_can_be_reset(self):
        operative = self.make_staff('cajero@x.com')
        teacher = self.make_staff('docente@x.com', Role.TEACHER)
        other_admin = self.make_staff('otro.admin@x.com', Role.ADMIN)
        self.reset(operative, teacher, other_admin)
        for user in (operative, teacher, other_admin):
            user.refresh_from_db()
            self.assertTrue(user.must_change_password, user.email)
        self.assertEqual(self.mail_count(), 3)

    def test_the_own_account_inactive_and_removed_accounts_are_skipped(self):
        inactive = self.make_staff('inactivo@x.com')
        inactive.is_active = False
        inactive.save(update_fields=['is_active'])
        old = self.root.password
        self.reset(self.root, inactive)
        self.root.refresh_from_db()
        self.assertEqual(self.root.password, old)
        self.assertEqual(self.mail_count(), 0)

    def test_more_than_the_limit_in_one_action_changes_nothing(self):
        users = [self.make_staff(f'u{i}@x.com') for i in range(26)]
        self.reset(*users)
        self.assertFalse(CustomUser.objects.filter(must_change_password=True).exists())
        self.assertEqual(self.mail_count(), 0)

    def test_the_audit_log_records_the_reset_without_any_secret(self):
        operative = self.make_staff('cajero@x.com')
        self.reset(operative)
        entry = AuditLog.objects.get(action=AuditLog.Action.PASSWORD_RESET)
        self.assertEqual((entry.actor, entry.target_id, entry.source), (self.root, operative.pk, 'ADMIN'))
        _recipient, _first, _label, _login, temporary = self.mail_args()
        self.assertNotIn(temporary, str(entry.detail))

    # -- helpers
    def mail_calls(self):
        from users.tasks import send_password_reset_by_admin_email
        return send_password_reset_by_admin_email.delay.call_args_list

    def mail_count(self):
        return len(self.mail_calls())

    def mail_args(self):
        return self.mail_calls()[-1].args


class RoleChangeTests(SuperadminTestCase):
    def change_url(self, user):
        return reverse('admin:users_customuser_change', args=[user.pk])

    def post_role(self, user, role):
        return self.client.post(self.change_url(user), {
            'email': user.email, 'first_name': 'ANA', 'last_name': 'PEREZ', 'institution': self.institution.pk,
            'is_active': 'on', 'role': role,
        })

    def test_staff_can_move_between_staff_roles_and_loses_their_sessions(self):
        user = self.make_staff('cajero@x.com')
        RefreshToken.for_user(user)
        response = self.post_role(user, Role.TEACHER)
        self.assertEqual(response.status_code, 302)
        user.refresh_from_db()
        self.assertEqual(user.role, Role.TEACHER)
        self.assertEqual(BlacklistedToken.objects.filter(token__user=user).count(), 1)
        entry = AuditLog.objects.get(action=AuditLog.Action.ROLE_CHANGED)
        self.assertEqual(entry.detail, {'from': Role.OPERATIONS_STAFF, 'to': Role.TEACHER})

    def test_a_teacher_with_credit_history_cannot_change_role(self):
        from pos.models import Sale
        teacher = self.make_staff('docente@x.com', Role.TEACHER)
        cashier = self.make_staff('cajero@x.com')
        Sale.objects.create(
            institution=self.institution, cashier=cashier, customer=teacher,
            payment_method='CREDIT', total_amount=Decimal('2.00'),
        )
        self.assertIn('crédito', role_change_error(teacher, Role.OPERATIONS_STAFF, self.root))
        self.post_role(teacher, Role.OPERATIONS_STAFF)
        teacher.refresh_from_db()
        self.assertEqual(teacher.role, Role.TEACHER)

    def test_parents_students_superadmins_and_oneself_never_change_role(self):
        parent_user, student_user = self.make_family()
        other_root = CustomUser.objects.create_superuser('root2', 'root2@x.com', PASSWORD)
        for user, new_role in ((parent_user, Role.ADMIN), (student_user, Role.TEACHER),
                               (other_root, Role.TEACHER), (self.root, Role.TEACHER)):
            self.assertIsNotNone(role_change_error(user, new_role, self.root), user.username)
        self.assertIsNone(role_change_error(self.make_staff('x@x.com'), Role.ADMIN, self.root))

    def test_leaving_the_teacher_role_clears_the_teacher_allergies(self):
        from catalog.models import Allergen
        teacher = self.make_staff('docente@x.com', Role.TEACHER)
        teacher.allergies.add(Allergen.objects.first())  # the allergens are seeded by a migration
        self.post_role(teacher, Role.OPERATIONS_STAFF)
        teacher.refresh_from_db()
        self.assertFalse(teacher.allergies.exists())

    def test_the_role_field_is_read_only_for_parents(self):
        from django.contrib import admin
        parent_user, _student = self.make_family()
        request = self.client.get(reverse('admin:index')).wsgi_request
        fields = admin.site._registry[CustomUser].get_readonly_fields(request, parent_user)
        self.assertIn('role', fields)


class InstitutionManagementTests(SuperadminTestCase):
    def change_url(self, institution):
        return reverse('admin:users_institution_change', args=[institution.pk])

    def post(self, institution, **overrides):
        data = {'name': institution.name, 'address': '', 'phone': '', 'is_active': 'on', 'suspension_reason': ''}
        data.update(overrides)
        if data['is_active'] is None:
            del data['is_active']
        return self.client.post(self.change_url(institution), data)

    def test_the_list_shows_per_sede_figures(self):
        from pos.models import Sale
        parent_user, _student = self.make_family()
        Wallet.objects.filter(student__user__username='hijo').update(balance=Decimal('7.50'))
        Sale.objects.create(
            institution=self.institution, cashier=self.make_staff('cajero@x.com'),
            payment_method='CASH', total_amount=Decimal('3.00'),
        )
        response = self.client.get(reverse('admin:users_institution_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '$ 7.50')
        self.assertContains(response, '$ 3.00 (1)')

    def test_details_are_editable(self):
        response = self.post(self.institution, address='Av. 1', phone='099')
        self.assertEqual(response.status_code, 302)
        self.institution.refresh_from_db()
        self.assertEqual((self.institution.address, self.institution.phone), ('Av. 1', '099'))
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.INSTITUTION_UPDATED).exists())

    def test_the_name_is_unique_ignoring_case(self):
        other = Institution.objects.create(name='El Sauce')
        response = self.post(other, name='martim cereré')
        self.assertEqual(response.status_code, 200)
        other.refresh_from_db()
        self.assertEqual(other.name, 'El Sauce')

    def test_suspending_needs_a_reason_and_is_logged(self):
        self.assertEqual(self.post(self.institution, is_active=None).status_code, 200)  # no reason: rejected
        self.institution.refresh_from_db()
        self.assertTrue(self.institution.is_active)
        response = self.post(self.institution, is_active=None, suspension_reason='Contrato terminado')
        self.assertEqual(response.status_code, 302)
        self.institution.refresh_from_db()
        self.assertFalse(self.institution.is_active)
        self.assertEqual(self.institution.suspension_reason, 'Contrato terminado')
        self.assertIsNotNone(self.institution.suspended_at)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.INSTITUTION_SUSPENDED).exists())

    def test_reactivating_clears_the_suspension(self):
        self.post(self.institution, is_active=None, suspension_reason='x')
        self.post(self.institution)
        self.institution.refresh_from_db()
        self.assertEqual((self.institution.is_active, self.institution.suspended_at), (True, None))
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.INSTITUTION_REACTIVATED).exists())

    def test_a_suspended_sede_takes_no_new_accounts_and_is_hidden_from_non_admins(self):
        self.post(self.institution, is_active=None, suspension_reason='x')
        response = self.client.post(reverse('admin:users_customuser_add'), {
            'email': 'nuevo@x.com', 'first_name': 'luis', 'last_name': 'mora', 'role': Role.TEACHER,
            'institution': self.institution.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CustomUser.objects.filter(email='nuevo@x.com').exists())

    def test_only_a_sede_nobody_depends_on_can_be_deleted(self):
        from django.test import RequestFactory
        from django.contrib import admin
        request = RequestFactory().get('/')
        request.user = self.root
        site_admin = admin.site._registry[Institution]
        self.make_staff('cajero@x.com')
        self.assertFalse(site_admin.has_delete_permission(request, self.institution))
        empty = Institution.objects.create(name='Vacía')
        self.assertTrue(site_admin.has_delete_permission(request, empty))
        self.assertFalse(site_admin.has_delete_permission(request))


class InstitutionApiTests(APITestCase):
    def setUp(self):
        self.active = Institution.objects.create(name='Activa')
        self.suspended = Institution.objects.create(name='Suspendida', is_active=False)
        self.parent = CustomUser.objects.create_user(
            username='padre', email='padre@x.com', password=PASSWORD, role=Role.PARENT,
        )
        self.admin = CustomUser.objects.create_user(
            username='adm', email='adm@x.com', password=PASSWORD, role=Role.ADMIN, institution=self.active,
        )

    def listing(self, user):
        self.client.force_authenticate(user)
        return {row['name']: row['is_active'] for row in self.client.get('/api/users/institutions/').data}

    def test_non_admins_only_see_active_sedes(self):
        self.assertEqual(self.listing(self.parent), {'Activa': True})

    def test_administrators_see_all_with_their_state(self):
        self.assertEqual(self.listing(self.admin), {'Activa': True, 'Suspendida': False})


class AuditLogTests(TestCase):
    def test_detail_never_keeps_secrets(self):
        entry = record_audit(
            AuditLog.Action.ACCOUNT_CREATED,
            detail={'role': 'TEACHER', 'temporary_password': 'abc123', 'refresh_token': 'x', 'code': '1234', 'ok': 1},
        )
        self.assertEqual(entry.detail, {'role': 'TEACHER', 'ok': 1})

    def test_a_failing_log_never_breaks_the_caller(self):
        with patch('users.audit.AuditLog.objects.create', side_effect=RuntimeError('db down')):
            self.assertIsNone(record_audit(AuditLog.Action.ACCOUNT_CREATED))

    def test_a_failed_task_is_recorded_without_its_arguments(self):
        from users.apps import record_task_failure

        class FakeTask:
            name = 'users.tasks.send_staff_credentials_email'

        record_task_failure(sender=FakeTask(), exception=ConnectionError('smtp'), args=('a@x.com', 'S3cret'))
        entry = AuditLog.objects.get(action=AuditLog.Action.TASK_FAILED)
        self.assertEqual(entry.source, AuditLog.Source.SYSTEM)
        self.assertEqual(entry.detail, {'task': FakeTask.name, 'exception': 'ConnectionError'})

    def test_api_staff_actions_are_logged(self):
        institution = Institution.objects.create(name='Martim Cereré')
        admin_user = CustomUser.objects.create_user(
            username='adm', email='adm@x.com', password=PASSWORD, role=Role.ADMIN, institution=institution,
        )
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(admin_user)
        created = client.post('/api/users/staff/', {
            'email': 'nuevo@x.com', 'first_name': 'Luis', 'last_name': 'Mora', 'role': Role.TEACHER,
        }, format='json')
        self.assertEqual(created.status_code, 201, created.data)
        user = CustomUser.objects.get(email='nuevo@x.com')
        client.delete(f'/api/users/staff/{user.pk}/')
        actions = list(AuditLog.objects.order_by('id').values_list('action', 'source'))
        self.assertEqual(actions, [('ACCOUNT_CREATED', 'API'), ('ACCOUNT_REMOVED', 'API')])


class SystemStatusTests(SuperadminTestCase):
    def test_the_page_lists_every_component(self):
        response = self.client.get(reverse('admin:system_status'))
        self.assertEqual(response.status_code, 200)
        for name in ('Base de datos', 'Caché', 'Workers de Celery', 'Migraciones', 'Archivos subidos'):
            self.assertContains(response, name)

    def test_a_failing_component_is_reported_without_breaking_the_page(self):
        with patch('core.health._broker', side_effect=ConnectionError('rabbit down')):
            response = self.client.get(reverse('admin:system_status'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'rabbit down')
        self.assertEqual(response.context['overall'], health.ERROR)

    def test_smtp_is_only_checked_on_demand(self):
        with patch('core.health._smtp', return_value=(health.OK, 'listo')) as smtp:
            self.client.get(reverse('admin:system_status'))
            smtp.assert_not_called()
            self.client.post(reverse('admin:system_status'), {'check_smtp': '1'})
            smtp.assert_called_once()

    def test_a_worker_less_queue_is_an_error(self):
        class Queue:
            message_count, consumer_count = 4, 0

        class Channel:
            def queue_declare(self, **kwargs):
                return Queue()

        class Conn:
            default_channel = Channel()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def ensure_connection(self, **kwargs):
                return None

        with patch('core.health._broker_connection', return_value=Conn()):
            status, detail = health._worker_queue()
        self.assertEqual(status, health.ERROR)
        self.assertIn('Sin workers', detail)

    def test_only_staff_can_open_the_page(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('admin:system_status')).status_code, 302)


class AlertsTests(SuperadminTestCase):
    def setUp(self):
        super().setUp()
        alerts.cache.clear()
        # createsuperuser leaves the administrator without a sede; give this one a sede so
        # that the "clean system" really is clean.
        self.root.institution = self.institution
        self.root.save(update_fields=['institution'])

    def titles(self):
        return [alert.title for alert in alerts.collect_alerts(force=True)]

    def test_a_clean_system_has_no_alerts(self):
        self.assertEqual(self.titles(), [])

    def test_accounts_left_half_created_raise_alerts(self):
        CustomUser.objects.create_user(username='huerfano', email='h@x.com', password=PASSWORD, role=Role.STUDENT)
        CustomUser.objects.create_user(username='padre2', email='p2@x.com', password=PASSWORD, role=Role.PARENT)
        found = ' | '.join(self.titles())
        self.assertIn('Estudiantes sin perfil', found)
        self.assertIn('Padres sin perfil', found)

    def test_student_without_wallet_and_sede_without_admin(self):
        parent_user, student_user = self.make_family()
        Wallet.objects.filter(student__user=student_user).delete()
        Institution.objects.create(name='Sede sin administrador')
        found = ' | '.join(self.titles())
        self.assertIn('Estudiantes sin billetera', found)
        self.assertIn('Sedes activas sin ningún administrador', found)

    def test_unused_temporary_passwords_and_failed_tasks_are_flagged(self):
        old = self.make_staff('docente@x.com', Role.TEACHER, pending=True)
        CustomUser.objects.filter(pk=old.pk).update(date_joined=timezone.now() - timedelta(days=10))
        AuditLog.objects.create(action=AuditLog.Action.TASK_FAILED, source='SYSTEM', detail={'task': 't'})
        found = ' | '.join(self.titles())
        self.assertIn('contraseña temporal sin usar', found)
        self.assertIn('Tareas en segundo plano fallidas', found)

    def test_the_index_shows_the_alerts_and_the_link_to_the_status_page(self):
        CustomUser.objects.create_user(username='huerfano', email='h@x.com', password=PASSWORD, role=Role.STUDENT)
        response = self.client.get(reverse('admin:index'))
        self.assertContains(response, 'Estudiantes sin perfil')
        self.assertContains(response, reverse('admin:system_status'))

    def test_a_broken_provider_does_not_hide_the_others(self):
        def broken():
            raise RuntimeError('boom')
            yield  # pragma: no cover

        CustomUser.objects.create_user(username='huerfano', email='h@x.com', password=PASSWORD, role=Role.STUDENT)
        with patch.object(alerts, '_providers', [broken, *alerts._providers]):
            self.assertTrue(any('sin perfil' in title for title in self.titles()))

    def test_problems_seen_by_the_status_page_become_alerts(self):
        with patch('core.health._broker', side_effect=ConnectionError('rabbit down')):
            self.client.get(reverse('admin:system_status'))
        self.assertTrue(any('rabbit down' in title for title in self.titles()))


class LastLoginTests(APITestCase):
    def test_an_api_login_records_the_last_access(self):
        user = CustomUser.objects.create_user(
            username='u@x.com', email='u@x.com', password=PASSWORD, role=Role.TEACHER,
            institution=Institution.objects.create(name='Martim Cereré'),
        )
        self.assertIsNone(user.last_login)
        response = self.client.post('/api/token/', {'username': 'u@x.com', 'password': PASSWORD}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        user.refresh_from_db()
        self.assertIsNotNone(user.last_login)
