import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock
import zoneinfo

import jwt
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from kombu.exceptions import OperationalError
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from catalog.models import DailyMenu, Ingredient, MenuDish, MenuItem, MonthlyMenu
from catalog.services import MonthlyMenuService
from pos.models import PreOrder
from users.models import CustomUser, Institution, ParentProfile, StudentProfile, UserBiometric

GIF = (
    b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04'
    b'\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
)


def _photo(name):
    return SimpleUploadedFile(name, GIF, content_type='image/gif')


class InstitutionSeparationTests(APITestCase):
    """Data is always separated per institution; only an ADMIN can join sedes."""

    def setUp(self):
        self.inst_a = Institution.objects.create(name='Sede A')
        self.inst_b = Institution.objects.create(name='Sede B')
        make = CustomUser.objects.create_user

        self.admin_a = make(username='admin_a', email='aa@x.com', password='x', role=CustomUser.Role.ADMIN, institution=self.inst_a)
        self.admin_b = make(username='admin_b', email='ab@x.com', password='x', role=CustomUser.Role.ADMIN, institution=self.inst_b)
        self.staff_a = make(username='staff_a', email='sa@x.com', password='x', role=CustomUser.Role.OPERATIONS_STAFF, institution=self.inst_a)
        self.staff_b = make(username='staff_b', email='sb@x.com', password='x', role=CustomUser.Role.OPERATIONS_STAFF, institution=self.inst_b)
        self.teacher_a = make(username='teacher_a', email='ta@x.com', password='x', role=CustomUser.Role.TEACHER, institution=self.inst_a)
        self.teacher_b = make(username='teacher_b', email='tb@x.com', password='x', role=CustomUser.Role.TEACHER, institution=self.inst_b)

        self.parent_user = make(username='parent', email='p@x.com', password='x', role=CustomUser.Role.PARENT)
        self.parent = ParentProfile.objects.create(user=self.parent_user)
        self.other_parent_user = make(username='parent2', email='p2@x.com', password='x', role=CustomUser.Role.PARENT)
        self.other_parent = ParentProfile.objects.create(user=self.other_parent_user)

        self.student_a_user = make(username='st_a', email='sta@x.com', password='x', role=CustomUser.Role.STUDENT)
        self.student_a = StudentProfile.objects.create(
            user=self.student_a_user, parent=self.parent, institution=self.inst_a, profile_picture=_photo('a.gif'))
        self.student_b_user = make(username='st_b', email='stb@x.com', password='x', role=CustomUser.Role.STUDENT)
        self.student_b = StudentProfile.objects.create(
            user=self.student_b_user, parent=self.other_parent, institution=self.inst_b, profile_picture=_photo('b.gif'))

        self.ing = Ingredient.objects.create(name='Arroz sep', allergens_reviewed=True)
        self.item_a = self._item(self.inst_a, 'Item A')
        self.item_b = self._item(self.inst_b, 'Item B')

    def _item(self, institution, name):
        item = MenuItem.objects.create(institution=institution, name=name, price=Decimal('1.00'), stock=5)
        item.ingredients.add(self.ing)
        return item

    def _auth(self, user):
        token = RefreshToken.for_user(user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.access_token}')

    def _names(self, response):
        data = response.data['results'] if isinstance(response.data, dict) else response.data
        return {row['name'] for row in data}

    # ---- product menu -------------------------------------------------
    def test_staff_and_teacher_menu_is_own_sede_and_ignores_all(self):
        for user in (self.staff_a, self.teacher_a):
            self._auth(user)
            self.assertEqual(self._names(self.client.get('/api/catalog/menu/?institution=all')), {'Item A'})

    def test_parent_menu_uses_child_sede(self):
        self._auth(self.parent_user)
        self.assertEqual(self._names(self.client.get(f'/api/catalog/menu/?student_id={self.student_a.id}')), {'Item A'})

    def test_parent_menu_requires_own_child(self):
        self._auth(self.parent_user)
        self.assertEqual(self.client.get('/api/catalog/menu/').status_code, 400)
        self.assertEqual(self.client.get(f'/api/catalog/menu/?student_id={self.student_b.id}').status_code, 403)

    def test_student_menu_is_own_sede(self):
        self._auth(self.student_b_user)
        self.assertEqual(self._names(self.client.get('/api/catalog/menu/')), {'Item B'})

    def test_admin_menu_default_one_sede_and_all_joins(self):
        self._auth(self.admin_a)
        self.assertEqual(self._names(self.client.get('/api/catalog/menu/admin/')), {'Item A'})
        self.assertEqual(self._names(self.client.get(f'/api/catalog/menu/admin/?institution={self.inst_b.id}')), {'Item B'})
        self.assertEqual(self._names(self.client.get('/api/catalog/menu/admin/?institution=all')), {'Item A', 'Item B'})
        self.assertEqual(self.client.get('/api/catalog/menu/admin/?institution=abc').status_code, 400)
        self.assertEqual(self.client.get('/api/catalog/menu/admin/?institution=9999').status_code, 400)

    def test_admin_edits_other_sede_item_only_when_targeting_it(self):
        self._auth(self.admin_a)
        base = f'/api/catalog/menu/{self.item_b.id}/'
        for suffix in ('', '?institution=all'):
            self.assertEqual(self.client.patch(base + suffix, {'name': 'hack'}).status_code, 404)
            self.assertEqual(self.client.delete(base + suffix).status_code, 404)
        ok = self.client.patch(f'{base}?institution={self.inst_b.id}', {'name': 'Renamed'})
        self.assertEqual(ok.status_code, 200, ok.data)
        self.item_b.refresh_from_db()
        self.assertEqual(self.item_b.name, 'Renamed')

    def _create_item(self, suffix=''):
        return self.client.post(
            f'/api/catalog/menu/create/{suffix}',
            {'name': 'Nuevo', 'price': '2.00', 'stock': 3, 'ingredients': [self.ing.id]},
            format='multipart',
        )

    def test_created_item_lands_in_targeted_sede_else_own(self):
        self._auth(self.admin_a)
        self.assertEqual(self._create_item(f'?institution={self.inst_b.id}').status_code, 201)
        self.assertEqual(MenuItem.objects.get(name='Nuevo').institution, self.inst_b)
        MenuItem.objects.filter(name='Nuevo').delete()
        for suffix in ('', '?institution=all'):
            self.assertEqual(self._create_item(suffix).status_code, 201)
            self.assertEqual(MenuItem.objects.get(name='Nuevo').institution, self.inst_a)
            MenuItem.objects.filter(name='Nuevo').delete()

    def test_write_with_invalid_institution_is_400(self):
        self._auth(self.admin_a)
        self.assertEqual(self._create_item('?institution=9999').status_code, 400)
        self.assertEqual(self._create_item('?institution=abc').status_code, 400)

    def test_non_admin_write_param_is_ignored(self):
        from core.mixins import resolve_write_institution
        from rest_framework.test import APIRequestFactory
        from rest_framework.request import Request
        request = Request(APIRequestFactory().post(f'/x/?institution={self.inst_b.id}'))
        request.user = self.staff_a
        self.assertEqual(resolve_write_institution(request), self.inst_a)

    def test_dish_and_monthly_menu_land_in_targeted_sede(self):
        self._auth(self.admin_a)
        dish = self.client.post(
            f'/api/catalog/dishes/?institution={self.inst_b.id}',
            {'name': 'Plato nuevo', 'price': '3.00', 'ingredients': [self.ing.id], 'image': _photo('dish.gif')},
            format='multipart',
        )
        self.assertEqual(dish.status_code, 201, dish.data)
        self.assertEqual(MenuDish.objects.get(name='Plato nuevo').institution, self.inst_b)

        menu = self.client.post(
            f'/api/catalog/monthly_menus/?institution={self.inst_b.id}',
            {'year': 2027, 'month': 1}, format='json',
        )
        self.assertEqual(menu.status_code, 201, menu.data)
        monthly = MonthlyMenu.objects.get(year=2027, month=1)
        self.assertEqual(monthly.institution, self.inst_b)
        # duplicate check runs against the target sede, not the admin's own
        self.assertEqual(self.client.post(
            f'/api/catalog/monthly_menus/?institution={self.inst_b.id}',
            {'year': 2027, 'month': 1}, format='json').status_code, 400)
        self.assertEqual(self.client.post(
            '/api/catalog/monthly_menus/', {'year': 2027, 'month': 1}, format='json').status_code, 201)

    def test_day_dishes_must_belong_to_target_sede(self):
        monthly = MonthlyMenuService.create_month(self.inst_b, 2027, 2, self.admin_b)
        day = monthly.days.first()
        dish_b = MenuDish.objects.create(institution=self.inst_b, name='DB', price=Decimal('1.00'))
        dish_a = MenuDish.objects.create(institution=self.inst_a, name='DA', price=Decimal('1.00'))
        url = f'/api/catalog/monthly_menus/{monthly.id}/days/{day.date.isoformat()}/'
        self._auth(self.admin_a)
        # no target: menu of B is invisible to A
        self.assertEqual(self.client.put(url, {'dish_ids': [dish_b.id]}, format='json').status_code, 404)
        target = f'?institution={self.inst_b.id}'
        self.assertEqual(self.client.put(url + target, {'dish_ids': [dish_a.id]}, format='json').status_code, 400)
        self.assertEqual(self.client.put(url + target, {'dish_ids': [dish_b.id]}, format='json').status_code, 200)

    def test_publish_and_unpublish_other_sede_with_target(self):
        monthly = MonthlyMenuService.create_month(self.inst_b, 2027, 3, self.admin_b)
        dish = MenuDish.objects.create(institution=self.inst_b, name='DB2', price=Decimal('1.00'))
        monthly.days.first().dishes.add(dish)
        self._auth(self.admin_a)
        base = f'/api/catalog/monthly_menus/{monthly.id}'
        self.assertEqual(self.client.post(f'{base}/publish/').status_code, 404)
        with mock.patch('catalog.services.send_monthly_menu_published_email.delay'):
            self.assertEqual(self.client.post(f'{base}/publish/?institution={self.inst_b.id}').status_code, 200)
        self.assertEqual(self.client.post(f'{base}/unpublish/?institution={self.inst_b.id}').status_code, 200)

    def test_non_admin_cannot_manage_items(self):
        self._auth(self.staff_a)
        self.assertEqual(self.client.get('/api/catalog/menu/admin/').status_code, 403)

    # ---- monthly menu ---------------------------------------------------
    def test_monthly_menus_admin_all_is_read_only_join(self):
        MonthlyMenuService.create_month(self.inst_a, 2026, 11, self.admin_a)
        MonthlyMenuService.create_month(self.inst_b, 2026, 11, self.admin_b)
        self._auth(self.admin_a)
        own = self.client.get('/api/catalog/monthly_menus/')
        joined = self.client.get('/api/catalog/monthly_menus/?institution=all')
        self.assertEqual(len(own.data), 1)
        self.assertEqual(len(joined.data), 2)

    # ---- POS menu for staff -------------------------------------------
    def _publish_menu(self, institution, admin, dish_name):
        dish = MenuDish.objects.create(institution=institution, name=dish_name, price=Decimal('2.00'))
        today = datetime.now(zoneinfo.ZoneInfo('America/Guayaquil')).date()
        menu = MonthlyMenu.objects.create(
            institution=institution, year=today.year, month=today.month,
            status=MonthlyMenu.Status.PUBLISHED, created_by=admin)
        day = DailyMenu.objects.create(monthly_menu=menu, date=today)
        day.dishes.add(dish)
        return day

    def test_pos_menu_only_for_students_of_operator_sede(self):
        self._publish_menu(self.inst_a, self.admin_a, 'Plato A')
        self._publish_menu(self.inst_b, self.admin_b, 'Plato B')
        self._auth(self.staff_a)
        ok = self.client.get(f'/api/catalog/pos/students/{self.student_a.id}/menu/')
        self.assertEqual(ok.status_code, 200)
        dishes = {d['name'] for day in ok.data for d in day['dishes']}
        self.assertEqual(dishes, {'Plato A'})
        self.assertEqual(self.client.get(f'/api/catalog/pos/students/{self.student_b.id}/menu/').status_code, 403)

    def test_pos_menu_denied_to_non_operators(self):
        self._auth(self.parent_user)
        self.assertEqual(self.client.get(f'/api/catalog/pos/students/{self.student_a.id}/menu/').status_code, 403)

    # ---- POS QR / deliver ----------------------------------------------
    def _qr_token(self, user):
        return jwt.encode(
            {'type': 'pos_dynamic_qr', 'user_id': user.id, 'nonce': uuid.uuid4().hex,
             'exp': datetime.now(zoneinfo.ZoneInfo('UTC')) + timedelta(minutes=5)},
            settings.SECRET_KEY, algorithm='HS256')

    def test_qr_identification_is_scoped_to_operator_sede(self):
        self._auth(self.staff_a)
        self.assertEqual(self.client.post('/api/pos/identify/qr/', {'token': self._qr_token(self.student_a_user)}, format='json').status_code, 200)
        self.assertEqual(self.client.post('/api/pos/identify/qr/', {'token': self._qr_token(self.student_b_user)}, format='json').status_code, 403)

    def test_deliver_is_scoped_to_operator_sede(self):
        day = self._publish_menu(self.inst_b, self.admin_b, 'Plato B')
        order = PreOrder.objects.create(student=self.student_b, daily_menu=day, total_amount=Decimal('2.00'))
        self._auth(self.staff_a)
        self.assertEqual(self.client.patch(f'/api/pos/preorders/{order.id}/deliver/').status_code, 403)
        self._auth(self.staff_b)
        self.assertEqual(self.client.patch(f'/api/pos/preorders/{order.id}/deliver/').status_code, 200)

    # ---- biometrics ----------------------------------------------------
    def _register(self, actor, target):
        self._auth(actor)
        with mock.patch('core.biometrics.BiometricValidationMixin.extract_face_embedding') as extract, \
                mock.patch('core.biometrics.BiometricValidationMixin.encrypt_embedding', return_value=(b'c', b'n', b't')):
            extract.return_value = [0.0]
            return self.client.post(
                '/api/users/biometrics/register/',
                {'photo': _photo('f.gif'), 'target_user_id': target.id},
                format='multipart',
            )

    def test_parent_registers_only_own_child(self):
        self.assertEqual(self._register(self.parent_user, self.student_a_user).status_code, 201)
        self.assertEqual(self._register(self.parent_user, self.student_b_user).status_code, 403)

    def test_admin_registers_only_teachers_of_own_sede(self):
        self.assertEqual(self._register(self.admin_a, self.teacher_a).status_code, 201)
        self.assertEqual(self._register(self.admin_a, self.teacher_b).status_code, 403)

    def test_biometrics_denied_for_other_roles_and_actors(self):
        self.assertEqual(self._register(self.admin_a, self.student_a_user).status_code, 403)
        self.assertEqual(self._register(self.staff_a, self.teacher_a).status_code, 403)
        self.assertEqual(self._register(self.admin_a, self.staff_a).status_code, 403)
        self.assertEqual(self._register(self.teacher_a, self.teacher_a).status_code, 403)
        self.assertFalse(UserBiometric.objects.filter(user=self.staff_a).exists())

    def test_match_face_requires_institution(self):
        from core.biometrics import BiometricValidationMixin
        with self.assertRaises(ValueError):
            BiometricValidationMixin().match_face(mock.Mock())

    # ---- publish with broker down --------------------------------------
    def test_publish_survives_broker_down(self):
        menu = MonthlyMenuService.create_month(self.inst_a, 2026, 12, self.admin_a)
        dish = MenuDish.objects.create(institution=self.inst_a, name='P', price=Decimal('1.00'))
        menu.days.first().dishes.add(dish)
        self._auth(self.admin_a)
        with mock.patch('catalog.services.send_monthly_menu_published_email.delay', side_effect=OperationalError('down')):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(f'/api/catalog/monthly_menus/{menu.id}/publish/')
        self.assertEqual(response.status_code, 200)
        menu.refresh_from_db()
        self.assertEqual(menu.status, MonthlyMenu.Status.PUBLISHED)
