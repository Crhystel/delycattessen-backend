from datetime import date
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from catalog.models import DailyMenu, Ingredient, MenuDish, MenuItem, MonthlyMenu
from pos.models import PreOrder, PreOrderItem, Sale, SaleItem
from users.models import CustomUser, Institution, ParentProfile, StudentProfile

GIF = (
    b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04'
    b'\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
)


def gif(name):
    return SimpleUploadedFile(name, GIF, content_type='image/gif')


class OrderPhotoTests(APITestCase):
    """The parent's order lists show a photo of each product."""

    def setUp(self):
        make = CustomUser.objects.create_user
        institution = Institution.objects.create(name='Sede A')
        self.parent_user = make(username='parent', email='p@x.com', password='x', role=CustomUser.Role.PARENT)
        parent = ParentProfile.objects.create(user=self.parent_user)
        self.student_user = make(username='student', email='s@x.com', password='x', role=CustomUser.Role.STUDENT)
        self.student = StudentProfile.objects.create(
            user=self.student_user, parent=parent, institution=institution, profile_picture=gif('s.gif'))

        ingredient = Ingredient.objects.create(name='Base', allergens_reviewed=True)
        self.dish = MenuDish.objects.create(
            institution=institution, name='Pollo asado', price=Decimal('3.50'), image=gif('pollo.gif'))
        self.dish.ingredients.add(ingredient)
        monthly = MonthlyMenu.objects.create(
            institution=institution, year=2026, month=11, status=MonthlyMenu.Status.PUBLISHED)
        daily = DailyMenu.objects.create(monthly_menu=monthly, date=date(2026, 11, 10))
        daily.dishes.add(self.dish)
        self.pre_order = PreOrder.objects.create(
            student=self.student, daily_menu=daily, total_amount=Decimal('3.50'))
        PreOrderItem.objects.create(
            pre_order=self.pre_order, dish=self.dish, quantity=1, price_at_purchase=Decimal('3.50'))

        self.with_photo = MenuItem.objects.create(
            institution=institution, name='Helado', price=Decimal('1.00'), stock=3, image=gif('helado.gif'))
        self.without_photo = MenuItem.objects.create(
            institution=institution, name='Pan', price=Decimal('0.50'), stock=3)
        cashier = make(username='cashier', email='c@x.com', password='x',
                       role=CustomUser.Role.OPERATIONS_STAFF, institution=institution)
        sale = Sale.objects.create(
            institution=institution, cashier=cashier, customer=self.student_user,
            payment_method=Sale.PaymentMethod.CASH, total_amount=Decimal('1.50'))
        SaleItem.objects.create(sale=sale, menu_item=self.with_photo, quantity=1, price_at_purchase=Decimal('1.00'))
        SaleItem.objects.create(sale=sale, menu_item=self.without_photo, quantity=1, price_at_purchase=Decimal('0.50'))

        self.client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.parent_user).access_token}')

    def test_preorder_items_carry_the_dish_photo(self):
        response = self.client.get('/api/pos/preorders/')
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data['results'] if isinstance(response.data, dict) else response.data
        image = rows[0]['items'][0]['image']
        self.assertTrue(image.startswith('http'), image)
        self.assertIn('pollo', image)

    def test_purchase_items_carry_the_product_photo_or_null(self):
        response = self.client.get('/api/pos/sales/history/')
        self.assertEqual(response.status_code, 200, response.data)
        by_name = {item['menu_item_name']: item['image'] for item in response.data[0]['items']}
        self.assertIn('helado', by_name['Helado'])
        self.assertIsNone(by_name['Pan'])
