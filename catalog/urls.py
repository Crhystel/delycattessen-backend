from django.urls import path
from .views import MenuListView

urlpatterns = [
    path('menu/', MenuListView.as_view(), name='menu-list'),
]
