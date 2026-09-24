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
    path('menu/', MenuListView.as_view(), name='menu-list'),
    path('menu/admin/', AdminMenuListView.as_view(), name='menu-admin-list'),
    path('menu/create/', MenuItemCreateView.as_view(), name='menu-create'),
    path('menu/<int:pk>/', MenuItemDetailView.as_view(), name='menu-detail'),
    path('ingredients/', IngredientListView.as_view(), name='ingredient-list'),
    path('ingredients/create/', IngredientCreateView.as_view(), name='ingredient-create'),
    path('ingredients/<int:pk>/', IngredientDetailView.as_view(), name='ingredient-detail'),
    path('allergens/', AllergenListView.as_view(), name='allergen-list'),
    path('allergens/create/', AllergenCreateView.as_view(), name='allergen-create'),
]