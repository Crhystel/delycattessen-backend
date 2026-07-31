from django.urls import path
from .views import RegistroPadreView, RegistroEstudianteView

urlpatterns = [
    path('registro-padre/', RegistroPadreView.as_view(), name='registro_padre'),
    path('registro-estudiante/', RegistroEstudianteView.as_view(), name='registro_estudiante'),
]
