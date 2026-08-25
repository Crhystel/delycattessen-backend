from rest_framework import generics
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from .models import MenuItem, Ingredient, Allergen
from .serializers import (
    MenuItemSerializer,
    IngredientSerializer,
    IngredientCreateSerializer,
    AllergenSerializer,
)


class MenuListView(generics.ListAPIView):
    queryset = MenuItem.objects.filter(is_active=True, is_visible=True)
    serializer_class = MenuItemSerializer
    permission_classes = [IsAuthenticated]


class MenuItemCreateView(generics.CreateAPIView):
    queryset = MenuItem.objects.all()
    serializer_class = MenuItemSerializer
    permission_classes = [IsAdminUser]


class MenuItemDetailView(generics.RetrieveUpdateDestroyAPIView):
    """GET/PATCH/DELETE /api/catalog/menu/<id>/ — used by the admin panel
    to edit or remove a product from the catalog."""

    queryset = MenuItem.objects.all()
    serializer_class = MenuItemSerializer
    permission_classes = [IsAdminUser]


class IngredientListView(generics.ListAPIView):
    queryset = Ingredient.objects.all()
    serializer_class = IngredientSerializer
    permission_classes = [IsAdminUser]


class IngredientCreateView(generics.CreateAPIView):
    """POST /api/catalog/ingredients/create/ — lets an admin add a new
    ingredient on the fly from the product form."""

    queryset = Ingredient.objects.all()
    serializer_class = IngredientCreateSerializer
    permission_classes = [IsAdminUser]


class AllergenListView(generics.ListAPIView):
    queryset = Allergen.objects.all().order_by('name')
    serializer_class = AllergenSerializer
    permission_classes = [IsAuthenticated]
    
class AdminMenuListView(generics.ListAPIView):
    """GET /api/catalog/menu/admin/ — returns ALL products (including
    hidden ones), used by the admin panel's product table so staff can
    still see and edit out-of-stock items."""
    queryset = MenuItem.objects.all().order_by('name')
    serializer_class = MenuItemSerializer
    permission_classes = [IsAdminUser]