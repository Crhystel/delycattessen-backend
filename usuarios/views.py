from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.viewsets import ModelViewSet
from django.contrib.auth import get_user_model
from django.utils.translation import gettext_lazy as _
from .serializers import (
    ParentRegistrationSerializer,
    StudentRegistrationSerializer,
    CreateStaffSerializer,
    PasswordResetRequestSerializer,
    PasswordResetConfirmSerializer,
    InstitutionSerializer,
)
from .permissions import IsAdministrator, SameInstitutionPermission, CanRequestPasswordReset
from .mixins import InstitutionScopeMixin, TokenGeneratorMixin
from .models import CustomUser, Institution
from rest_framework_simplejwt.views import TokenObtainPairView
from .serializers import EmailTokenObtainSerializer


class ParentRegistrationView(generics.CreateAPIView):
    serializer_class = ParentRegistrationSerializer
    permission_classes = [AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(
            {"mensaje": str(_('Padre registrado exitosamente.')), "email": serializer.data.get("email")},
            status=status.HTTP_201_CREATED,
            headers=headers
        )


class StudentRegistrationView(generics.CreateAPIView):
    serializer_class = StudentRegistrationSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(
            {"mensaje": str(_('Estudiante registrado exitosamente.')), "username": serializer.data.get("username")},
            status=status.HTTP_201_CREATED,
            headers=headers
        )


class StaffViewSet(InstitutionScopeMixin, ModelViewSet):
    """
    Allows the Administrator to create, list, and view staff
    (Teachers and Operations Staff), with cross-institution scope.
    """
    serializer_class = CreateStaffSerializer
    permission_classes = [IsAuthenticated, IsAdministrator, SameInstitutionPermission]
    queryset = CustomUser.objects.filter(
        role__in=[CustomUser.Role.TEACHER, CustomUser.Role.OPERATIONS_STAFF]
    )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(
            {
                "mensaje": f"{user.get_role_display()} creado exitosamente.",
                "email": user.email,
                "institucion": user.institution.name,
                "password_temporal": user._temporary_password,
                "nota": str(_('El usuario deberá cambiar esta contraseña en su primer inicio de sesión.'))
            },
            status=status.HTTP_201_CREATED
        )
class InstitutionListView(generics.ListAPIView):
    """
    Read-only endpoint that lists all institutions. Used by the
    Administrator's frontend to populate the institution selector
    for cross-institution scope.
    """
    serializer_class = InstitutionSerializer
    permission_classes = [IsAuthenticated, IsAdministrator]
    queryset = Institution.objects.all().order_by('name')

class BasePasswordResetView(generics.GenericAPIView):
    permission_classes = [CanRequestPasswordReset]
    success_message = _('Operación exitosa')

    def process_recovery_action(self, serializer):
        raise NotImplementedError('You must implement process_recovery_action')

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.process_recovery_action(serializer)
        return Response(
            {"mensaje": str(self.success_message)},
            status=status.HTTP_200_OK
        )


class RequestPasswordResetView(TokenGeneratorMixin, BasePasswordResetView):
    serializer_class = PasswordResetRequestSerializer
    success_message = _('Si el correo existe en nuestra base de datos, se han enviado las instrucciones de recuperación.')

    def process_recovery_action(self, serializer):
        email = serializer.validated_data['email']
        user = get_user_model().objects.get(email=email, is_active=True)
        self.generate_and_send_token(user, email)


class ConfirmPasswordResetView(BasePasswordResetView):
    serializer_class = PasswordResetConfirmSerializer
    success_message = _('La contraseña ha sido restablecida exitosamente.')

    def process_recovery_action(self, serializer):
        serializer.save()
        
class EmailTokenObtainPairView(TokenObtainPairView):
    serializer_class = EmailTokenObtainSerializer