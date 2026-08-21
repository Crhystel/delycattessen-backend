from django.urls import path
from .views import MenuListView, MenuItemCreateView, IngredientListView

urlpatterns = [
    path('menu/', MenuListView.as_view(), name='menu-list'),
    path('menu/create/', MenuItemCreateView.as_view(), name='menu-create'),
    path('ingredients/', IngredientListView.as_view(), name='ingredient-list'),
]
