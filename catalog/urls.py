from django.urls import path
from .views import (
    AllergenCreateView,
    MenuListView,
    MenuItemCreateView,
    MenuItemDetailView,
    AdminMenuListView,
    IngredientListView,
    IngredientCreateView,
    IngredientDetailView,
    AllergenListView,
)

urlpatterns = [
    path('menu/', MenuListView.as_view(), name='menu_list'),
    path('menu/admin/', AdminMenuListView.as_view(), name='menu_admin_list'),
    path('menu/create/', MenuItemCreateView.as_view(), name='menu_create'),
    path('menu/<int:pk>/', MenuItemDetailView.as_view(), name='menu_detail'),
    path('ingredients/', IngredientListView.as_view(), name='ingredient_list'),
    path('ingredients/create/', IngredientCreateView.as_view(), name='ingredient_create'),
    path('ingredients/<int:pk>/', IngredientDetailView.as_view(), name='ingredient_detail'),
    path('allergens/', AllergenListView.as_view(), name='allergen_list'),
    path('allergens/create/', AllergenCreateView.as_view(), name='allergen_create'),
]