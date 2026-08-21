from django.urls import path
from .views import PreOrderCreateView

urlpatterns = [
    path('preorder/', PreOrderCreateView.as_view(), name='preorder-create'),
]
