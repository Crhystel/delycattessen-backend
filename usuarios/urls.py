from django.urls import path
from .views import (
    RegistroPadreView, 
    RegistroEstudianteView,
    SolicitarRecuperacionView,
    ConfirmarRecuperacionView
)

urlpatterns = [
    path('registro-padre/', RegistroPadreView.as_view(), name='registro_padre'),
    path('registro-estudiante/', RegistroEstudianteView.as_view(), name='registro_estudiante'),
    path('recuperar-password/solicitar/', SolicitarRecuperacionView.as_view(), name='recuperar_password_solicitar'),
    path('recuperar-password/confirmar/', ConfirmarRecuperacionView.as_view(), name='recuperar_password_confirmar'),
]
