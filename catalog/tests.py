from decimal import Decimal
from unittest import mock
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from users.models import CustomUser, Institution, ParentProfile, StudentProfile
from catalog.models import Allergen, Ingredient, MenuItem, MenuDish, MonthlyMenu, DailyMenu
from catalog.services import MonthlyMenuService
from pos.models import PreOrder, PreOrderItem


def _dummy_image(name='dish.jpg'):
    return SimpleUploadedFile(
        name=name,
        content=b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b',
        content_type='image/gif',
    )


class MonthlyMenuTests(APITestCase):
    def setUp(self):
        # 1. Institutions
        self.institution_a = Institution.objects.create(name="Colegio Americano")
        self.institution_b = Institution.objects.create(name="Colegio Menor")

        # 2. Users
        self.admin_a = CustomUser.objects.create_user(
            username="admin_a", email="admin_a@delycattessen.com",
            password="testpassword123", role=CustomUser.Role.ADMIN,
            institution=self.institution_a,
        )
        self.admin_b = CustomUser.objects.create_user(
            username="admin_b", email="admin_b@delycattessen.com",
            password="testpassword123", role=CustomUser.Role.ADMIN,
            institution=self.institution_b,
        )
        self.teacher_a = CustomUser.objects.create_user(
            username="teacher_a", email="teacher_a@delycattessen.com",
            password="testpassword123", role=CustomUser.Role.TEACHER,
            institution=self.institution_a,
        )

        # Parents & Children
        self.parent_user_a = CustomUser.objects.create_user(
            username="parent_a", email="parent_a@gmail.com",
            password="testpassword123", role=CustomUser.Role.PARENT,
            first_name="Carlos", last_name="Perez",
        )
        self.parent_profile_a = ParentProfile.objects.create(user=self.parent_user_a)

        self.student_user_a = CustomUser.objects.create_user(
            username="student_a", email="student_a@gmail.com",
            password="testpassword123", role=CustomUser.Role.STUDENT,
        )
        self.student_profile_a = StudentProfile.objects.create(
            user=self.student_user_a,
            parent=self.parent_profile_a,
            institution=self.institution_a,
            profile_picture=_dummy_image('student_a.jpg'),
        )

        self.parent_user_b = CustomUser.objects.create_user(
            username="parent_b", email="parent_b@gmail.com",
            password="testpassword123", role=CustomUser.Role.PARENT,
            first_name="Maria", last_name="Lopez",
        )
        self.parent_profile_b = ParentProfile.objects.create(user=self.parent_user_b)

        self.student_user_b = CustomUser.objects.create_user(
            username="student_b", email="student_b@gmail.com",
            password="testpassword123", role=CustomUser.Role.STUDENT,
        )
        self.student_profile_b = StudentProfile.objects.create(
            user=self.student_user_b,
            parent=self.parent_profile_b,
            institution=self.institution_b,
            profile_picture=_dummy_image('student_b.jpg'),
        )

        # Ingredients & Allergens
        self.allergen_gluten, _ = Allergen.objects.get_or_create(
            name="Gluten", defaults={"description": "Cereales con gluten"}
        )
        self.ingredient_reviewed, _ = Ingredient.objects.get_or_create(
            name="Harina de trigo especial", defaults={"allergens_reviewed": True}
        )
        self.ingredient_reviewed.allergens.add(self.allergen_gluten)

        self.ingredient_unreviewed, _ = Ingredient.objects.get_or_create(
            name="Leche entera especial", defaults={"allergens_reviewed": False}
        )
        self.ingredient_unreviewed.allergens_reviewed = False
        self.ingredient_unreviewed.save(update_fields=['allergens_reviewed'])

    def _auth(self, user):
        token = RefreshToken.for_user(user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.access_token}')

    def test_01_institution_scoping_prevents_cross_access_404(self):
        """1. Admin de la institución A no puede ver/editar menús ni platillos de la institución B (404)."""
        # Create dish and menu in Institution A
        dish_a = MenuDish.objects.create(
            institution=self.institution_a,
            name="Hamburguesa Institucional",
            price=Decimal("4.50"),
            image=_dummy_image("dish_a.jpg"),
        )
        dish_a.ingredients.add(self.ingredient_reviewed)

        menu_a = MonthlyMenuService.create_month(
            institution=self.institution_a,
            year=2026,
            month=10,
            user=self.admin_a,
        )

        # Admin B attempts to access Institution A's resources
        self._auth(self.admin_b)

        # Dish detail
        res_dish = self.client.get(f'/api/catalog/dishes/{dish_a.id}/')
        self.assertEqual(res_dish.status_code, status.HTTP_404_NOT_FOUND)

        res_patch = self.client.patch(f'/api/catalog/dishes/{dish_a.id}/', {'name': 'Nombre cambiado'})
        self.assertEqual(res_patch.status_code, status.HTTP_404_NOT_FOUND)

        res_del = self.client.delete(f'/api/catalog/dishes/{dish_a.id}/')
        self.assertEqual(res_del.status_code, status.HTTP_404_NOT_FOUND)

        # Menu detail
        res_menu = self.client.get(f'/api/catalog/monthly_menus/{menu_a.id}/')
        self.assertEqual(res_menu.status_code, status.HTTP_404_NOT_FOUND)

        # Menu publish
        res_pub = self.client.post(f'/api/catalog/monthly_menus/{menu_a.id}/publish/')
        self.assertEqual(res_pub.status_code, status.HTTP_404_NOT_FOUND)

    def test_02_non_admin_user_receives_403(self):
        """2. Un usuario que no es ADMIN recibe 403."""
        self._auth(self.teacher_a)

        res_dishes = self.client.get('/api/catalog/dishes/')
        self.assertEqual(res_dishes.status_code, status.HTTP_403_FORBIDDEN)

        res_menus = self.client.get('/api/catalog/monthly_menus/')
        self.assertEqual(res_menus.status_code, status.HTTP_403_FORBIDDEN)

        res_create = self.client.post('/api/catalog/monthly_menus/', {'year': 2026, 'month': 11})
        self.assertEqual(res_create.status_code, status.HTTP_403_FORBIDDEN)

    def test_03_create_monthly_menu_generates_business_days_and_rejects_duplicate(self):
        """3. Crear menú genera solo días lunes–viernes del mes; crear un segundo menú del mismo mes → 400."""
        self._auth(self.admin_a)

        # November 2026 has 30 days, 21 weekdays (Mon-Fri)
        res = self.client.post('/api/catalog/monthly_menus/', {'year': 2026, 'month': 11})
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        menu = MonthlyMenu.objects.get(institution=self.institution_a, year=2026, month=11)
        self.assertEqual(menu.status, MonthlyMenu.Status.DRAFT)
        days = menu.days.all()
        self.assertEqual(days.count(), 21)

        # Verify every generated date is Monday (0) through Friday (4)
        for day in days:
            self.assertLess(day.date.weekday(), 5)

        # Second attempt for the same month and institution -> 400
        res_duplicate = self.client.post('/api/catalog/monthly_menus/', {'year': 2026, 'month': 11})
        self.assertEqual(res_duplicate.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Ya existe un menú mensual para ese mes.', str(res_duplicate.data))

    def test_04_dish_with_unreviewed_ingredient_rejected_400(self):
        """4. Platillo con un ingrediente sin allergens_reviewed → 400."""
        self._auth(self.admin_a)

        data = {
            'name': 'Pasta con Crema',
            'description': 'Pasta con salsa blanca',
            'price': '3.75',
            'image': _dummy_image('pasta.jpg'),
            'ingredients': [self.ingredient_unreviewed.id],
        }
        res = self.client.post('/api/catalog/dishes/', data, format='multipart')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('ingredients', res.data)

    def test_05_publish_without_dishes_fails_400(self):
        """5. Publicar sin días configurados → 400."""
        self._auth(self.admin_a)

        menu = MonthlyMenuService.create_month(
            institution=self.institution_a,
            year=2026,
            month=11,
            user=self.admin_a,
        )

        res = self.client.post(f'/api/catalog/monthly_menus/{menu.id}/publish/')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Asigna platillos al menos a un día antes de publicar.', str(res.data))

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_06_publish_enqueues_task_and_sends_email_only_to_relevant_parents(self):
        """6. Publicar encola la tarea y envía correo solo a padres con hijos en esa institución."""
        self._auth(self.admin_a)

        # Create dish
        dish = MenuDish.objects.create(
            institution=self.institution_a,
            name="Almuerzo Completo",
            price=Decimal("4.00"),
            image=_dummy_image("almuerzo.jpg"),
        )
        dish.ingredients.add(self.ingredient_reviewed)

        # Create menu and assign dish to first business day
        menu = MonthlyMenuService.create_month(
            institution=self.institution_a,
            year=2026,
            month=11,
            user=self.admin_a,
        )
        first_day = menu.days.first()

        res_assign = self.client.put(
            f'/api/catalog/monthly_menus/{menu.id}/days/{first_day.date.strftime("%Y-%m-%d")}/',
            {'dish_ids': [dish.id]},
            format='json',
        )
        self.assertEqual(res_assign.status_code, status.HTTP_200_OK)

        # Publish and verify celery delay is called
        with mock.patch('catalog.services.send_monthly_menu_published_email.delay') as mock_delay:
            with self.captureOnCommitCallbacks(execute=True):
                res_publish = self.client.post(f'/api/catalog/monthly_menus/{menu.id}/publish/')
            self.assertEqual(res_publish.status_code, status.HTTP_200_OK)
            self.assertEqual(res_publish.data['status'], MonthlyMenu.Status.PUBLISHED)
            mock_delay.assert_called_once_with(menu.id)

        # Directly execute the task to verify email delivery filtering
        from catalog.tasks import send_monthly_menu_published_email
        mail.outbox.clear()
        send_monthly_menu_published_email(menu.id)

        # Verify only parent_a received an email
        self.assertEqual(len(mail.outbox), 1)
        sent_email = mail.outbox[0]
        self.assertEqual(sent_email.to, [self.parent_user_a.email])
        self.assertNotIn(self.parent_user_b.email, [m.to[0] for m in mail.outbox])
        self.assertNotIn(self.teacher_a.email, [m.to[0] for m in mail.outbox])

        self.assertIn("Nuevo menú mensual publicado - D'Elycattessen", sent_email.subject)
        self.assertIn("Noviembre 2026", sent_email.body)
        self.assertIn(self.institution_a.name, sent_email.body)

    def test_07_unpublish_and_modification_with_and_without_preorders(self):
        """7. Despublicar con preórdenes → 400; sin preórdenes → 200."""
        self._auth(self.admin_a)

        dish = MenuDish.objects.create(
            institution=self.institution_a,
            name="Sándwich Saludable",
            price=Decimal("2.50"),
            image=_dummy_image("sandwich.jpg"),
        )
        dish.ingredients.add(self.ingredient_reviewed)

        menu = MonthlyMenuService.create_month(
            institution=self.institution_a,
            year=2026,
            month=11,
            user=self.admin_a,
        )
        first_day = menu.days.first()
        first_day.dishes.add(dish)

        # Publish menu
        with mock.patch('catalog.services.send_monthly_menu_published_email.delay'):
            with self.captureOnCommitCallbacks(execute=True):
                MonthlyMenuService.publish(menu)

        # Unpublish without preorders -> 200
        res_unpub = self.client.post(f'/api/catalog/monthly_menus/{menu.id}/unpublish/')
        self.assertEqual(res_unpub.status_code, status.HTTP_200_OK)
        self.assertEqual(res_unpub.data['status'], MonthlyMenu.Status.DRAFT)

        # Publish again
        with mock.patch('catalog.services.send_monthly_menu_published_email.delay'):
            with self.captureOnCommitCallbacks(execute=True):
                MonthlyMenuService.publish(menu)

        # Create a preorder for first_day
        preorder = PreOrder.objects.create(
            student=self.student_profile_a,
            daily_menu=first_day,
            total_amount=Decimal("2.50"),
            status=PreOrder.Status.PENDING,
        )
        PreOrderItem.objects.create(
            pre_order=preorder,
            dish=dish,
            quantity=1,
            price_at_purchase=Decimal("2.50"),
        )

        # Unpublish with preorders -> 400
        res_unpub_blocked = self.client.post(f'/api/catalog/monthly_menus/{menu.id}/unpublish/')
        self.assertEqual(res_unpub_blocked.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('No se puede pasar a borrador: el menú ya tiene preórdenes.', str(res_unpub_blocked.data))

        # Modifying a day with preorders -> 400
        res_mod_day = self.client.put(
            f'/api/catalog/monthly_menus/{menu.id}/days/{first_day.date.strftime("%Y-%m-%d")}/',
            {'dish_ids': []},
            format='json',
        )
        self.assertEqual(res_mod_day.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Este día ya tiene preórdenes y no se puede modificar.', str(res_mod_day.data))

        # Modifying or deleting a dish with pending preorders -> 400
        res_mod_dish = self.client.patch(
            f'/api/catalog/dishes/{dish.id}/',
            {'name': 'Nuevo Nombre'},
            format='json',
        )
        self.assertEqual(res_mod_dish.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Este platillo tiene preórdenes pendientes y no se puede modificar.', str(res_mod_dish.data))

        res_del_dish = self.client.delete(f'/api/catalog/dishes/{dish.id}/')
        self.assertEqual(res_del_dish.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Este platillo tiene preórdenes pendientes y no se puede modificar.', str(res_del_dish.data))
