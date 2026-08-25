from rest_framework import generics
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from .models import MenuItem, Ingredient, Allergen
from .serializers import MenuItemSerializer, IngredientSerializer, AllergenSerializer

class MenuListView(generics.ListAPIView):
    # Se filtran los ocultos por stock == 0
    queryset = MenuItem.objects.filter(is_active=True, is_visible=True)
    serializer_class = MenuItemSerializer
    permission_classes = [IsAuthenticated]

class MenuItemCreateView(generics.CreateAPIView):
    queryset = MenuItem.objects.all()
    serializer_class = MenuItemSerializer
    permission_classes = [IsAdminUser]

class IngredientListView(generics.ListAPIView):
    queryset = Ingredient.objects.all()
    serializer_class = IngredientSerializer
    permission_classes = [IsAdminUser]

class AllergenListView(generics.ListAPIView):
    queryset = Allergen.objects.all().order_by('name')
    serializer_class = AllergenSerializer
    permission_classes = [IsAuthenticated]