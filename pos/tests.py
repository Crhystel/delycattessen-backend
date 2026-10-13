import uuid
import jwt
import numpy as np
from datetime import datetime, timezone, timedelta
from django.test import TestCase
from django.conf import settings
from rest_framework.test import APIClient
from rest_framework import status
from users.models import CustomUser, Institution, UserBiometric
from pos.mixins import BiometricValidationMixin

class BiometricEncryptionTests(TestCase, BiometricValidationMixin):
    def test_aes_gcm_encryption_roundtrip(self):
        """Verifies that 128-d float embeddings encrypt and decrypt losslessly."""
        original_vector = np.random.randn(128).astype(np.float32)
        ciphertext, nonce, tag = self.encrypt_embedding(original_vector)

        # Ciphertext must differ from raw vector
        self.assertNotEqual(ciphertext, original_vector.tobytes())
        self.assertEqual(len(nonce), 12)
        self.assertEqual(len(tag), 16)

        decrypted_vector = self.decrypt_embedding(ciphertext, nonce, tag)
        np.testing.assert_almost_equal(original_vector, decrypted_vector, decimal=5)

    def test_cosine_similarity_identical_vectors(self):
        """Identical vectors must have similarity close to 1.0."""
        vec = np.random.randn(128).astype(np.float32)
        sim = self.calculate_similarity(vec, vec)
        self.assertAlmostEqual(sim, 1.0, places=4)


class DynamicQRIdentificationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.inst = Institution.objects.create(name="Test Inst")
        self.operator = CustomUser.objects.create_user(
            username="cashier",
            email="cashier@test.com",
            password="password123",
            role=CustomUser.Role.OPERATIONS_STAFF,
            institution=self.inst
        )
        self.student = CustomUser.objects.create_user(
            username="student1",
            email="student1@test.com",
            password="password123",
            role=CustomUser.Role.STUDENT,
            institution=self.inst,
            first_name="Carlitos",
            last_name="Perez"
        )


    def test_dynamic_qr_identification_success(self):
        """Tests identifying student with a valid dynamic QR token."""
        self.client.force_authenticate(user=self.operator)
        nonce = uuid.uuid4().hex
        payload = {
            'sub': str(self.student.id),
            'user_id': self.student.id,
            'username': self.student.username,
            'role': self.student.role,
            'nonce': nonce,
            'type': 'pos_dynamic_qr',
            'iat': datetime.now(timezone.utc),
            'exp': datetime.now(timezone.utc) + timedelta(seconds=60),
        }
        token = jwt.encode(payload, settings.SECRET_KEY, algorithm='HS256')

        response = self.client.post('/api/pos/identify/qr/', {'token': token}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['user_id'], self.student.id)
        self.assertEqual(response.data['full_name'], "Carlitos Perez")
        self.assertEqual(response.data['identification_method'], "DYNAMIC_QR")

    def test_dynamic_qr_single_use_prevents_replay(self):
        """Tests that reusing the same QR nonce is rejected to prevent spoofing."""
        self.client.force_authenticate(user=self.operator)
        nonce = uuid.uuid4().hex
        payload = {
            'sub': str(self.student.id),
            'user_id': self.student.id,
            'username': self.student.username,
            'role': self.student.role,
            'nonce': nonce,
            'type': 'pos_dynamic_qr',
            'iat': datetime.now(timezone.utc),
            'exp': datetime.now(timezone.utc) + timedelta(seconds=60),
        }

        token = jwt.encode(payload, settings.SECRET_KEY, algorithm='HS256')

        # First use -> OK
        res1 = self.client.post('/api/pos/identify/qr/', {'token': token}, format='json')
        self.assertEqual(res1.status_code, status.HTTP_200_OK)

        # Replay attempt -> 400 Bad Request
        res2 = self.client.post('/api/pos/identify/qr/', {'token': token}, format='json')
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("ya fue utilizado", res2.data['detail'])

    def test_non_operator_cannot_access_pos_identify(self):
        """Verifies IsOperativeUser rejects student/parent accounts."""
        self.client.force_authenticate(user=self.student)
        response = self.client.post('/api/pos/identify/qr/', {'token': 'dummy'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


from decimal import Decimal
import zoneinfo
from django.core.files.uploadedfile import SimpleUploadedFile
from users.models import ParentProfile, StudentProfile, ParentalControl
from wallet.models import Wallet, Transaction
from catalog.models import Allergen, Ingredient, MenuDish, MonthlyMenu, DailyMenu
from pos.models import PreOrder, PreOrderItem


def _dummy_image(name='dish.jpg'):
    return SimpleUploadedFile(
        name=name,
        content=b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b',
        content_type='image/gif',
    )


class PreOrderMonthlyMenuTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.institution = Institution.objects.create(name="Colegio San Gabriel")

        # Parent
        self.parent_user = CustomUser.objects.create_user(
            username="parent_gabriel",
            email="parent@sangabriel.edu.ec",
            password="password123",
            role=CustomUser.Role.PARENT,
        )
        self.parent_profile = ParentProfile.objects.create(user=self.parent_user)

        # Other Parent
        self.other_parent_user = CustomUser.objects.create_user(
            username="other_parent",
            email="other@sangabriel.edu.ec",
            password="password123",
            role=CustomUser.Role.PARENT,
        )
        self.other_parent_profile = ParentProfile.objects.create(user=self.other_parent_user)

        # Student
        self.student_user = CustomUser.objects.create_user(
            username="student_gabriel",
            email="student@sangabriel.edu.ec",
            password="password123",
            role=CustomUser.Role.STUDENT,
            first_name="Mateo",
            last_name="Morales",
        )
        self.student_profile = StudentProfile.objects.create(
            user=self.student_user,
            parent=self.parent_profile,
            institution=self.institution,
        )
        self.wallet = Wallet.objects.create(
            student=self.student_profile,
            balance=Decimal('50.00'),
            low_balance_threshold=Decimal('5.00'),
        )
        self.parental_control = ParentalControl.objects.create(
            student=self.student_profile,
            daily_limit_enabled=False,
            daily_limit_amount=Decimal('10.00'),
            allowed_days_enabled=False,
            allowed_days=[0, 1, 2, 3, 4],
        )

        # Cashier
        self.cashier = CustomUser.objects.create_user(
            username="cashier_pos",
            email="cashier@sangabriel.edu.ec",
            password="password123",
            role=CustomUser.Role.OPERATIONS_STAFF,
            institution=self.institution,
        )

        # Allergens & Ingredients
        self.allergen_mani, _ = Allergen.objects.get_or_create(name="Maní")
        self.allergen_lactosa, _ = Allergen.objects.get_or_create(name="Lactosa")

        self.ing_arroz, _ = Ingredient.objects.get_or_create(
            name="Arroz Preorder Test",
            defaults={'allergens_reviewed': True}
        )
        self.ing_mani, _ = Ingredient.objects.get_or_create(
            name="Mantequilla Maní Test",
            defaults={'allergens_reviewed': True}
        )
        self.ing_mani.allergens.add(self.allergen_mani)

        # Menu Dishes
        self.dish_pollo = MenuDish.objects.create(
            institution=self.institution,
            name="Seco de Pollo",
            description="Pollo con arroz y maduro",
            price=Decimal('3.50'),
            image=_dummy_image('pollo.jpg'),
            is_active=True,
        )
        self.dish_pollo.ingredients.add(self.ing_arroz)

        self.dish_mani = MenuDish.objects.create(
            institution=self.institution,
            name="Postre de Maní",
            description="Postre con crema de maní",
            price=Decimal('2.00'),
            image=_dummy_image('mani.jpg'),
            is_active=True,
        )
        self.dish_mani.ingredients.add(self.ing_mani)
        self.dish_mani.allergens.add(self.allergen_mani)

        self.dish_other = MenuDish.objects.create(
            institution=self.institution,
            name="Ensalada Verde",
            description="Ensalada fresca",
            price=Decimal('2.50'),
            image=_dummy_image('ensalada.jpg'),
            is_active=True,
        )
        self.dish_other.ingredients.add(self.ing_arroz)

        # Dates setup in America/Guayaquil
        self.today_ec = datetime.now(zoneinfo.ZoneInfo('America/Guayaquil')).date()
        self.future_date = self.today_ec + timedelta(days=2)

        # Published Monthly Menu covering future_date
        self.published_monthly = MonthlyMenu.objects.create(
            institution=self.institution,
            year=self.future_date.year,
            month=self.future_date.month,
            status=MonthlyMenu.Status.PUBLISHED,
        )
        self.daily_menu_future = DailyMenu.objects.create(
            monthly_menu=self.published_monthly,
            date=self.future_date,
        )
        self.daily_menu_future.dishes.add(self.dish_pollo, self.dish_mani)

        # Ensure today DailyMenu exists under published monthly menu for POS testing
        if self.today_ec.year == self.future_date.year and self.today_ec.month == self.future_date.month:
            self.today_monthly = self.published_monthly
        else:
            self.today_monthly = MonthlyMenu.objects.create(
                institution=self.institution,
                year=self.today_ec.year,
                month=self.today_ec.month,
                status=MonthlyMenu.Status.PUBLISHED,
            )
        self.daily_menu_today, _ = DailyMenu.objects.get_or_create(
            monthly_menu=self.today_monthly,
            date=self.today_ec,
        )
        self.daily_menu_today.dishes.add(self.dish_pollo)

    def test_preorder_success_published_future_date(self):
        """1. Preorden exitosa sobre menú publicado con fecha futura."""
        self.client.force_authenticate(user=self.parent_user)
        payload = {
            "student_id": self.student_profile.id,
            "delivery_date": str(self.future_date),
            "items": [
                {"dish_id": self.dish_pollo.id, "quantity": 2}
            ]
        }
        response = self.client.post('/api/pos/preorder/', payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn('pre_order_id', response.data)

        # Verify wallet balance discounted (50 - 7 = 43)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('43.00'))

        # Verify PreOrder record
        pre_order = PreOrder.objects.get(id=response.data['pre_order_id'])
        self.assertEqual(pre_order.daily_menu, self.daily_menu_future)
        self.assertEqual(pre_order.delivery_date, self.future_date)
        self.assertEqual(pre_order.total_amount, Decimal('7.00'))
        self.assertEqual(pre_order.status, PreOrder.Status.PENDING)

        # Verify PreOrderItem record
        item = pre_order.items.first()
        self.assertEqual(item.dish, self.dish_pollo)
        self.assertEqual(item.quantity, 2)
        self.assertEqual(item.price_at_purchase, Decimal('3.50'))

        # Verify Transaction record
        tx = Transaction.objects.get(pre_order=pre_order)
        self.assertEqual(tx.amount, Decimal('7.00'))
        self.assertEqual(tx.status, Transaction.Status.SUCCESS)
        self.assertEqual(tx.type, Transaction.Type.CONSUMPTION)

    def test_preorder_past_or_today_fails(self):
        """2. Intento de preordenar para hoy o fecha pasada -> 400."""
        self.client.force_authenticate(user=self.parent_user)
        # Attempt today
        res_today = self.client.post('/api/pos/preorder/', {
            "student_id": self.student_profile.id,
            "delivery_date": str(self.today_ec),
            "items": [{"dish_id": self.dish_pollo.id, "quantity": 1}]
        }, format='json')
        self.assertEqual(res_today.status_code, status.HTTP_400_BAD_REQUEST)

        # Attempt yesterday
        res_past = self.client.post('/api/pos/preorder/', {
            "student_id": self.student_profile.id,
            "delivery_date": str(self.today_ec - timedelta(days=1)),
            "items": [{"dish_id": self.dish_pollo.id, "quantity": 1}]
        }, format='json')
        self.assertEqual(res_past.status_code, status.HTTP_400_BAD_REQUEST)

    def test_preorder_draft_menu_fails(self):
        """3. Intento de preordenar sobre menú en BORRADOR -> 400."""
        self.client.force_authenticate(user=self.parent_user)
        from datetime import date
        draft_date = date(self.future_date.year + 1, 1, 15)
        draft_monthly = MonthlyMenu.objects.create(
            institution=self.institution,
            year=draft_date.year,
            month=draft_date.month,
            status=MonthlyMenu.Status.DRAFT,
        )
        draft_daily = DailyMenu.objects.create(monthly_menu=draft_monthly, date=draft_date)
        draft_daily.dishes.add(self.dish_pollo)

        res = self.client.post('/api/pos/preorder/', {
            "student_id": self.student_profile.id,
            "delivery_date": str(draft_date),
            "items": [{"dish_id": self.dish_pollo.id, "quantity": 1}]
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("No hay un menú publicado", str(res.data))

    def test_preorder_dish_not_in_daily_menu_fails(self):
        """4. Intento de preordenar un platillo que no está en el DailyMenu de esa fecha -> 400."""
        self.client.force_authenticate(user=self.parent_user)
        res = self.client.post('/api/pos/preorder/', {
            "student_id": self.student_profile.id,
            "delivery_date": str(self.future_date),
            "items": [{"dish_id": self.dish_other.id, "quantity": 1}]
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("no está disponible en el menú de esa fecha", str(res.data))

    def test_preorder_foreign_child_fails(self):
        """5. Intento de preordenar para un hijo ajeno -> 403."""
        self.client.force_authenticate(user=self.other_parent_user)
        res = self.client.post('/api/pos/preorder/', {
            "student_id": self.student_profile.id,
            "delivery_date": str(self.future_date),
            "items": [{"dish_id": self.dish_pollo.id, "quantity": 1}]
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_preorder_parental_control_blocked_day(self):
        """6. Control parental: día no permitido (allowed_days) -> 400."""
        self.client.force_authenticate(user=self.parent_user)
        self.parental_control.allowed_days_enabled = True
        # Allow only different weekday
        other_weekday = (self.future_date.weekday() + 1) % 7
        self.parental_control.allowed_days = [other_weekday]
        self.parental_control.save()

        res = self.client.post('/api/pos/preorder/', {
            "student_id": self.student_profile.id,
            "delivery_date": str(self.future_date),
            "items": [{"dish_id": self.dish_pollo.id, "quantity": 1}]
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("No está permitido comprar para ese día: está bloqueado por el control parental.", str(res.data))

    def test_preorder_parental_control_daily_limit_exceeded(self):
        """7. Control parental: supera límite diario para esa fecha -> 400."""
        self.client.force_authenticate(user=self.parent_user)
        self.parental_control.daily_limit_enabled = True
        self.parental_control.daily_limit_amount = Decimal('8.00')
        self.parental_control.save()

        # First order of 7.00 succeeds (7 <= 8)
        res1 = self.client.post('/api/pos/preorder/', {
            "student_id": self.student_profile.id,
            "delivery_date": str(self.future_date),
            "items": [{"dish_id": self.dish_pollo.id, "quantity": 2}]
        }, format='json')
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)

        # Second order of 3.50 for same date exceeds 8.00 (7 + 3.50 = 10.50 > 8)
        res2 = self.client.post('/api/pos/preorder/', {
            "student_id": self.student_profile.id,
            "delivery_date": str(self.future_date),
            "items": [{"dish_id": self.dish_pollo.id, "quantity": 1}]
        }, format='json')
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("La preorden supera el límite de gasto diario configurado para ese día.", str(res2.data))

    def test_preorder_allergen_blocked(self):
        """8. Alérgeno: platillo con alérgeno del estudiante -> 400 (bloqueo absoluto)."""
        self.client.force_authenticate(user=self.parent_user)
        self.student_profile.allergies.add(self.allergen_mani)

        res = self.client.post('/api/pos/preorder/', {
            "student_id": self.student_profile.id,
            "delivery_date": str(self.future_date),
            "items": [{"dish_id": self.dish_mani.id, "quantity": 1}]
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("contiene alérgenos que afectan al estudiante", str(res.data))

    def test_pos_identification_only_today_and_deliver(self):
        """9. POS: identificación solo incluye preórdenes de HOY y entrega valida fecha."""
        # Create preorder for tomorrow/future
        pre_order_future = PreOrder.objects.create(
            student=self.student_profile,
            daily_menu=self.daily_menu_future,
            total_amount=Decimal('3.50'),
            status=PreOrder.Status.PENDING,
        )
        PreOrderItem.objects.create(
            pre_order=pre_order_future,
            dish=self.dish_pollo,
            quantity=1,
            price_at_purchase=Decimal('3.50'),
        )

        # Create preorder for today
        pre_order_today = PreOrder.objects.create(
            student=self.student_profile,
            daily_menu=self.daily_menu_today,
            total_amount=Decimal('3.50'),
            status=PreOrder.Status.PENDING,
        )
        PreOrderItem.objects.create(
            pre_order=pre_order_today,
            dish=self.dish_pollo,
            quantity=1,
            price_at_purchase=Decimal('3.50'),
        )

        # Cashier identifies student via QR
        self.client.force_authenticate(user=self.cashier)
        nonce = uuid.uuid4().hex
        payload = {
            'sub': str(self.student_user.id),
            'user_id': self.student_user.id,
            'username': self.student_user.username,
            'role': self.student_user.role,
            'nonce': nonce,
            'type': 'pos_dynamic_qr',
            'iat': datetime.now(timezone.utc),
            'exp': datetime.now(timezone.utc) + timedelta(seconds=300),
        }
        token = jwt.encode(payload, settings.SECRET_KEY, algorithm='HS256')
        res_ident = self.client.post('/api/pos/identify/qr/', {'token': token}, format='json')
        self.assertEqual(res_ident.status_code, status.HTTP_200_OK)

        # Pending orders must ONLY contain today's preorder
        pending_ids = [po['pre_order_id'] for po in res_ident.data['pending_orders']]
        self.assertIn(pre_order_today.id, pending_ids)
        self.assertNotIn(pre_order_future.id, pending_ids)

        # Trying to deliver future preorder -> 400 with formatted date
        res_future_deliver = self.client.patch(f'/api/pos/preorders/{pre_order_future.id}/deliver/')
        self.assertEqual(res_future_deliver.status_code, status.HTTP_400_BAD_REQUEST)
        formatted_date = self.future_date.strftime('%d/%m/%Y')
        self.assertIn(f"Esta preorden es para el {formatted_date}, no para hoy.", str(res_future_deliver.data))

        # Delivering today preorder -> 200
        res_today_deliver = self.client.patch(f'/api/pos/preorders/{pre_order_today.id}/deliver/')
        self.assertEqual(res_today_deliver.status_code, status.HTTP_200_OK)
        pre_order_today.refresh_from_db()
        self.assertEqual(pre_order_today.status, PreOrder.Status.DELIVERED)

    def test_student_monthly_menu_parent_endpoint(self):
        """10. Consulta de menú mensual por parte del padre."""
        self.client.force_authenticate(user=self.parent_user)
        self.student_profile.allergies.add(self.allergen_mani)

        res = self.client.get(f'/api/catalog/students/{self.student_profile.id}/monthly_menu/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        # Must return list of days with date > today
        day_dates = [d['date'] for d in res.data]
        self.assertIn(str(self.future_date), day_dates)
        self.assertNotIn(str(self.today_ec), day_dates)

        # Check future day contents
        future_day_data = next(d for d in res.data if d['date'] == str(self.future_date))
        self.assertFalse(future_day_data['is_blocked_by_parental_control'])
        self.assertIsNone(future_day_data['blocked_reason'])

        dish_mani_data = next(dish for dish in future_day_data['dishes'] if dish['id'] == self.dish_mani.id)
        self.assertTrue(dish_mani_data['has_student_allergens'])
        self.assertIn("Maní", dish_mani_data['allergens'])

        dish_pollo_data = next(dish for dish in future_day_data['dishes'] if dish['id'] == self.dish_pollo.id)
        self.assertFalse(dish_pollo_data['has_student_allergens'])



class LazyBiometricImportsTests(TestCase):
    """cv2/numpy must stay out of the web process until a face request needs them."""

    def test_loading_urls_does_not_import_cv2_or_numpy(self):
        import subprocess
        import sys
        from django.conf import settings

        code = (
            "import os, sys, django\n"
            "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'delycattessen_backend.settings')\n"
            "django.setup()\n"
            "import delycattessen_backend.urls, pos.mixins\n"
            "sys.exit(int('cv2' in sys.modules) + 2 * int('numpy' in sys.modules))\n"
        )
        result = subprocess.run(
            [sys.executable, '-c', code], cwd=settings.BASE_DIR, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr[-500:])


class BiometricModelReuseAndFilteringTests(TestCase, BiometricValidationMixin):
    def test_biometric_models_created_once_on_multiple_calls(self):
        """Verifies that FaceDetectorYN and FaceRecognizerSF are created only once."""
        from unittest.mock import patch, MagicMock
        import pos.mixins as mixins_module

        # Reset module globals to ensure a clean test
        with mixins_module._BIOMETRIC_LOCK:
            orig_detector = mixins_module._FACE_DETECTOR
            orig_recognizer = mixins_module._FACE_RECOGNIZER
            mixins_module._FACE_DETECTOR = None
            mixins_module._FACE_RECOGNIZER = None

        try:
            mock_detector = MagicMock()
            # Return one detected face: [x, y, w, h, ...]
            mock_detector.detect.return_value = (1, np.array([[10, 10, 50, 50]], dtype=np.float32))
            mock_recognizer = MagicMock()
            mock_recognizer.alignCrop.return_value = np.zeros((112, 112, 3), dtype=np.uint8)
            mock_recognizer.feature.return_value = np.ones((1, 128), dtype=np.float32)

            with patch('cv2.FaceDetectorYN.create', return_value=mock_detector) as mock_create_det, \
                 patch('cv2.FaceRecognizerSF.create', return_value=mock_recognizer) as mock_create_rec:

                # Dummy 100x100 RGB image encoded as JPEG
                import cv2
                _, img_encoded = cv2.imencode('.jpg', np.zeros((100, 100, 3), dtype=np.uint8))
                img_bytes = img_encoded.tobytes()

                # Call extract_face_embedding twice
                emb1 = self.extract_face_embedding(img_bytes)
                emb2 = self.extract_face_embedding(img_bytes)

                self.assertEqual(mock_create_det.call_count, 1)
                self.assertEqual(mock_create_rec.call_count, 1)
                self.assertEqual(len(emb1), 128)
                self.assertEqual(len(emb2), 128)
        finally:
            with mixins_module._BIOMETRIC_LOCK:
                mixins_module._FACE_DETECTOR = orig_detector
                mixins_module._FACE_RECOGNIZER = orig_recognizer

    def test_match_face_filters_by_institution(self):
        """match_face must not return a matched user from another institution."""
        inst_a = Institution.objects.create(name="Colegio A")
        inst_b = Institution.objects.create(name="Colegio B")

        user_a = CustomUser.objects.create_user(
            username="student_a", email="student_a@test.com", password="password123",
            role=CustomUser.Role.STUDENT, institution=inst_a
        )
        user_b = CustomUser.objects.create_user(
            username="student_b", email="student_b@test.com", password="password123",
            role=CustomUser.Role.STUDENT, institution=inst_b
        )

        from users.models import StudentProfile, ParentProfile
        parent_user = CustomUser.objects.create_user(
            username="parent_ab", email="parent_ab@test.com", password="password123",
            role=CustomUser.Role.PARENT
        )
        parent_profile = ParentProfile.objects.create(user=parent_user)

        StudentProfile.objects.create(user=user_a, institution=inst_a, parent=parent_profile)
        StudentProfile.objects.create(user=user_b, institution=inst_b, parent=parent_profile)

        # Vector of 128 dimensions normalized
        vector = np.ones(128, dtype=np.float32)
        vector /= np.linalg.norm(vector)

        ciphertext_b, nonce_b, tag_b = self.encrypt_embedding(vector)
        UserBiometric.objects.create(
            user=user_b,
            encrypted_embedding=ciphertext_b,
            nonce=nonce_b,
            tag=tag_b,
            is_active=True
        )

        # Searching with operator of institution A: user_b from institution B must NOT match
        matched_user, score = self.match_face(vector, institution=inst_a)
        self.assertIsNone(matched_user)
        self.assertEqual(score, -1.0)

        # Searching with operator of institution B: user_b MUST match
        matched_user_b, score_b = self.match_face(vector, institution=inst_b)
        self.assertIsNotNone(matched_user_b)
        self.assertEqual(matched_user_b.id, user_b.id)
        self.assertGreater(score_b, 0.9)

