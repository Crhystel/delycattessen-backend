from datetime import datetime, timedelta

from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.timezone import local_today
from core.mixins import OwnInstitutionMixin, ViewerInstitutionMixin
from users.models import StudentProfile
from users.permissions import IsAdministrator, IsOperativeUser, IsParentOfStudent, SameInstitutionAsTarget

from .mixins import PromotionsInForceMixin
from .models import Allergen, Combo, DailyMenu, Ingredient, MenuDish, MenuItem, MonthlyMenu, Promotion
from .serializers import (
    AllergenCreateSerializer,
    AllergenSerializer,
    ComboSerializer,
    DailyMenuSerializer,
    DailyMenuUpdateSerializer,
    IngredientCreateSerializer,
    IngredientSerializer,
    MenuDishSerializer,
    MenuItemSerializer,
    MonthlyMenuCreateSerializer,
    MonthlyMenuSerializer,
    PromotionSerializer,
    StudentDailyMenuSerializer,
)
from .services import ComboService, MonthlyMenuService, PromotionService


class MenuListView(PromotionsInForceMixin, ViewerInstitutionMixin, generics.ListAPIView):
    """Products of a single institution; see ViewerInstitutionMixin for how
    it is chosen per role (parents pass ?student_id=)."""
    queryset = MenuItem.objects.filter(is_active=True, is_visible=True)
    serializer_class = MenuItemSerializer
    permission_classes = [IsAuthenticated]


class AdminMenuListView(PromotionsInForceMixin, OwnInstitutionMixin, generics.ListAPIView):
    """Only active products: a "deleted" (is_active=False) product should
    disappear from the admin catalog too, not just from the parent-facing
    menu. The row stays in the database so historical PreOrderItem records
    keep a valid reference — see MenuItemDetailView.perform_destroy."""

    queryset = MenuItem.objects.filter(is_active=True).order_by('name')
    serializer_class = MenuItemSerializer
    permission_classes = [IsAdministrator]


class MenuItemCreateView(OwnInstitutionMixin, generics.CreateAPIView):
    queryset = MenuItem.objects.all()
    serializer_class = MenuItemSerializer
    permission_classes = [IsAdministrator]

    def perform_create(self, serializer):
        serializer.save(institution=self.get_user_institution())


class MenuItemDetailView(OwnInstitutionMixin, generics.RetrieveUpdateDestroyAPIView):
    """DELETE never hard-deletes: it soft-deletes by flipping is_active
    (and is_visible) to False. A real DB delete would fail anyway once the
    product has any PreOrderItem history (FK is on_delete=PROTECT), and
    even for a brand-new product with zero orders, soft delete keeps the
    behavior consistent and predictable for the admin."""

    queryset = MenuItem.objects.all()
    serializer_class = MenuItemSerializer
    permission_classes = [IsAdministrator]

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.is_visible = False
        instance.save()


class PromotionListCreateView(OwnInstitutionMixin, generics.ListCreateAPIView):
    """
    GET, POST /api/catalog/promotions/
    Lists the promotions of the admin's institution (newest first) or creates
    one. A product already in a promotion with overlapping dates is rejected
    (PromotionSerializer -> PromotionService.validate_products_available).
    """
    queryset = Promotion.objects.select_related('institution').prefetch_related('products')
    serializer_class = PromotionSerializer
    permission_classes = [IsAdministrator]

    def perform_create(self, serializer):
        serializer.save(institution=self.get_user_institution(), created_by=self.request.user)


class PromotionDetailView(OwnInstitutionMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    GET, PATCH, DELETE /api/catalog/promotions/<int:pk>/
    Deleting a promotion is a real delete: sales keep their own
    price_at_purchase, so no history depends on the promotion row.
    """
    queryset = Promotion.objects.select_related('institution').prefetch_related('products')
    serializer_class = PromotionSerializer
    permission_classes = [IsAdministrator]


class ActivePromotionListView(ViewerInstitutionMixin, generics.ListAPIView):
    """
    GET /api/catalog/promotions/active/
    Promotions in force today for the viewer's institution (parents pass
    ?student_id=, as in MenuListView), used by the mobile promotion banner.
    """
    queryset = Promotion.objects.select_related('institution').prefetch_related('products')
    serializer_class = PromotionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # Evaluated per request so a promotion disappears the day after it ends.
        return super().get_queryset().in_force(PromotionService.today())


class ComboListCreateView(OwnInstitutionMixin, generics.ListCreateAPIView):
    """
    GET, POST /api/catalog/combos/
    Combos of the admin's institution (newest first), or creates one.
    """
    queryset = Combo.objects.select_related('institution').prefetch_related('items__menu_item')
    serializer_class = ComboSerializer
    permission_classes = [IsAdministrator]

    def perform_create(self, serializer):
        serializer.save(institution=self.get_user_institution(), created_by=self.request.user)


class ComboDetailView(OwnInstitutionMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    GET, PATCH, DELETE /api/catalog/combos/<int:pk>/
    Deleting is a real delete: sold combos keep their own name and price.
    """
    queryset = Combo.objects.select_related('institution').prefetch_related('items__menu_item')
    serializer_class = ComboSerializer
    permission_classes = [IsAdministrator]


class ActiveComboListView(ViewerInstitutionMixin, generics.ListAPIView):
    """
    GET /api/catalog/combos/active/
    Combos in force today for the viewer's institution (parents pass
    ?student_id=): the banner of the app and the combos the POS can sell.
    """
    queryset = Combo.objects.select_related('institution')
    serializer_class = ComboSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return ComboService.in_force(super().get_queryset())


class IngredientListView(generics.ListAPIView):
    queryset = Ingredient.objects.all()
    serializer_class = IngredientSerializer
    permission_classes = [IsAdministrator | IsAdminUser]


class IngredientCreateView(generics.CreateAPIView):
    queryset = Ingredient.objects.all()
    serializer_class = IngredientCreateSerializer
    permission_classes = [IsAdministrator | IsAdminUser]


class IngredientDetailView(generics.RetrieveUpdateAPIView):
    """GET/PATCH /api/catalog/ingredients/<id>/ — lets an admin view or
    update which allergens a given ingredient contains."""

    queryset = Ingredient.objects.all()
    serializer_class = IngredientSerializer
    permission_classes = [IsAdministrator | IsAdminUser]


class AllergenListView(generics.ListAPIView):
    queryset = Allergen.objects.all().order_by('name')
    serializer_class = AllergenSerializer
    permission_classes = [IsAuthenticated]

class AllergenCreateView(generics.CreateAPIView):
    """POST /api/catalog/allergens/create/ — a parent can register a new
    allergen if it's not in the catalog yet (e.g. an ingredient-specific
    allergy like "Arroz" that isn't a standard medical category)."""

    queryset = Allergen.objects.all()
    serializer_class = AllergenCreateSerializer
    permission_classes = [IsAuthenticated]



class MenuDishListCreateView(OwnInstitutionMixin, generics.ListCreateAPIView):
    """
    GET, POST /api/catalog/dishes/
    Lists active monthly menu dishes or creates a new dish for the admin's institution.
    """
    serializer_class = MenuDishSerializer
    permission_classes = [IsAdministrator]
    queryset = MenuDish.objects.all()

    def get_queryset(self):
        return super().get_queryset().filter(is_active=True).order_by('name')

    def perform_create(self, serializer):
        serializer.save(institution=self.get_user_institution())


class MenuDishDetailView(OwnInstitutionMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    GET, PATCH, DELETE /api/catalog/dishes/<int:pk>/
    Updates or soft-deletes a dish. Fails if the dish has pending pre-orders.
    """
    serializer_class = MenuDishSerializer
    permission_classes = [IsAdministrator]
    queryset = MenuDish.objects.all()

    def ensure_no_pending_preorders(self, dish):
        if MonthlyMenuService.dish_has_pending_preorders(dish):
            raise ValidationError(_('Este platillo tiene preórdenes pendientes y no se puede modificar.'))

    def update(self, request, *args, **kwargs):
        self.ensure_no_pending_preorders(self.get_object())
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        self.ensure_no_pending_preorders(instance)
        instance.is_active = False
        instance.save(update_fields=['is_active', 'updated_at'])
        return Response(status=status.HTTP_204_NO_CONTENT)


class MonthlyMenuListCreateView(OwnInstitutionMixin, generics.ListCreateAPIView):
    """
    GET, POST /api/catalog/monthly_menus/
    Lists monthly menus of the admin's institution or creates a new draft menu
    with business days generated automatically via MonthlyMenuService.
    """
    permission_classes = [IsAdministrator]
    queryset = MonthlyMenu.objects.all()

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .prefetch_related('days__dishes', 'days__pre_orders')
            .order_by('-year', '-month')
        )

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return MonthlyMenuCreateSerializer
        return MonthlyMenuSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        institution = self.get_user_institution()
        monthly_menu = MonthlyMenuService.create_month(
            institution=institution,
            year=serializer.validated_data['year'],
            month=serializer.validated_data['month'],
            user=request.user,
        )
        output_serializer = MonthlyMenuSerializer(monthly_menu, context=self.get_serializer_context())
        return Response(output_serializer.data, status=status.HTTP_201_CREATED)


class MonthlyMenuDetailView(OwnInstitutionMixin, generics.RetrieveAPIView):
    """
    GET /api/catalog/monthly_menus/<int:pk>/
    Retrieves full details of a monthly menu including daily menus, dishes, and preorders.
    """
    permission_classes = [IsAdministrator]
    queryset = MonthlyMenu.objects.all().prefetch_related('days__dishes', 'days__pre_orders')
    serializer_class = MonthlyMenuSerializer


class DailyMenuUpdateView(OwnInstitutionMixin, generics.GenericAPIView):
    """
    PUT /api/catalog/monthly_menus/<int:pk>/days/<str:date>/
    Updates dishes assigned to a given date (YYYY-MM-DD) in the monthly menu.
    """
    permission_classes = [IsAdministrator]
    queryset = MonthlyMenu.objects.all()

    def put(self, request, pk, date, *args, **kwargs):
        monthly_menu = self.get_object()
        try:
            parsed_date = datetime.strptime(date, '%Y-%m-%d').date()
        except ValueError:
            raise ValidationError({'date': _('Formato de fecha inválido. Utilice YYYY-MM-DD.')})

        daily_menu = get_object_or_404(DailyMenu, monthly_menu=monthly_menu, date=parsed_date)
        serializer = DailyMenuUpdateSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)

        dishes = list(MenuDish.objects.filter(
            id__in=serializer.validated_data['dish_ids'],
            institution=monthly_menu.institution,
        ))
        MonthlyMenuService.set_day_dishes(daily_menu, dishes)
        return Response(DailyMenuSerializer(daily_menu).data, status=status.HTTP_200_OK)


class MonthlyMenuPublishView(OwnInstitutionMixin, generics.GenericAPIView):
    """
    POST /api/catalog/monthly_menus/<int:pk>/publish/
    Publishes the monthly menu and enqueues email notifications to parents.
    """
    permission_classes = [IsAdministrator]
    queryset = MonthlyMenu.objects.all()
    serializer_class = MonthlyMenuSerializer

    def post(self, request, *args, **kwargs):
        monthly_menu = self.get_object()
        published_menu = MonthlyMenuService.publish(monthly_menu)
        serializer = self.get_serializer(published_menu)
        return Response(serializer.data, status=status.HTTP_200_OK)


class MonthlyMenuUnpublishView(OwnInstitutionMixin, generics.GenericAPIView):
    """
    POST /api/catalog/monthly_menus/<int:pk>/unpublish/
    Reverts a published monthly menu back to DRAFT if no pre-orders exist.
    """
    permission_classes = [IsAdministrator]
    queryset = MonthlyMenu.objects.all()
    serializer_class = MonthlyMenuSerializer

    def post(self, request, *args, **kwargs):
        monthly_menu = self.get_object()
        unpublished_menu = MonthlyMenuService.unpublish(monthly_menu)
        serializer = self.get_serializer(unpublished_menu)
        return Response(serializer.data, status=status.HTTP_200_OK)


class BaseStudentMenuView(APIView):
    """
    Template Method: published menu of a student's own institution, with
    parental-control blockers and allergen alerts. Subclasses only vary the
    permission, the first valid date (`first_date`) and the extra object check.
    """

    def first_date(self, today):
        return today

    def check_student(self, request, student):
        self.check_object_permissions(request, student)

    def get(self, request, student_id):
        student = get_object_or_404(
            StudentProfile.objects.select_related('institution', 'parental_control').prefetch_related('allergies'),
            id=student_id,
        )
        self.check_student(request, student)
        today_ec = local_today()
        days = DailyMenu.objects.filter(
            monthly_menu__institution=student.institution,
            monthly_menu__status=MonthlyMenu.Status.PUBLISHED,
            date__gte=self.first_date(today_ec),
        ).prefetch_related(
            Prefetch('dishes', queryset=MenuDish.objects.filter(is_active=True).prefetch_related('allergens'))
        ).order_by('date')

        context = {
            'request': request,
            'student': student,
            'student_allergy_ids': set(student.allergies.values_list('id', flat=True)),
        }
        serializer = StudentDailyMenuSerializer(days, many=True, context=context)
        return Response(serializer.data, status=status.HTTP_200_OK)


class StudentMonthlyMenuView(BaseStudentMenuView):
    """
    GET /api/catalog/students/<int:student_id>/monthly_menu/
    Parent view: business days after today from published monthly menus of
    the child's institution.
    """
    permission_classes = [IsParentOfStudent]

    def first_date(self, today):
        return today + timedelta(days=1)


class POSStudentMenuView(BaseStudentMenuView):
    """
    GET /api/catalog/pos/students/<int:student_id>/menu/
    Staff view used to choose dishes for an identified student: published
    menus from today onward, restricted to the operator's own institution
    (an admin may widen it with ?institution=all).
    """
    permission_classes = [IsOperativeUser, SameInstitutionAsTarget]
