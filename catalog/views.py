from rest_framework import generics
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from .models import MenuItem, Ingredient, Allergen
from .serializers import (
    MenuItemSerializer,
    IngredientSerializer,
    IngredientCreateSerializer,
    AllergenSerializer,
    AllergenCreateSerializer,
)


class MenuListView(generics.ListAPIView):
    queryset = MenuItem.objects.filter(is_active=True, is_visible=True)
    serializer_class = MenuItemSerializer
    permission_classes = [IsAuthenticated]


class AdminMenuListView(generics.ListAPIView):
    queryset = MenuItem.objects.all().order_by('name')
    serializer_class = MenuItemSerializer
    permission_classes = [IsAdminUser]


class MenuItemCreateView(generics.CreateAPIView):
    queryset = MenuItem.objects.all()
    serializer_class = MenuItemSerializer
    permission_classes = [IsAdminUser]


class MenuItemDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = MenuItem.objects.all()
    serializer_class = MenuItemSerializer
    permission_classes = [IsAdminUser]


class IngredientListView(generics.ListAPIView):
    queryset = Ingredient.objects.all()
    serializer_class = IngredientSerializer
    permission_classes = [IsAdminUser]


class IngredientCreateView(generics.CreateAPIView):
    queryset = Ingredient.objects.all()
    serializer_class = IngredientCreateSerializer
    permission_classes = [IsAdminUser]


class IngredientDetailView(generics.RetrieveUpdateAPIView):
    """GET/PATCH /api/catalog/ingredients/<id>/ — lets an admin view or
    update which allergens a given ingredient contains."""

    queryset = Ingredient.objects.all()
    serializer_class = IngredientSerializer
    permission_classes = [IsAdminUser]


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