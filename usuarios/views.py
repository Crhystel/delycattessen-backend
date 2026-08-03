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
)
from .permissions import IsAdministrator, SameInstitutionPermission, CanRequestPasswordReset
from .mixins import InstitutionIsolationMixin, TokenGeneratorMixin
from .models import CustomUser


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


class StaffViewSet(InstitutionIsolationMixin, ModelViewSet):
    """
    Allows the Administrator to create, list, and view staff
    (Teachers and Operations Staff) at their own institution.
    """
    serializer_class = CreateStaffSerializer
    permission_classes = [IsAuthenticated, IsAdministrator, SameInstitutionPermission]

    def get_queryset(self):
        return CustomUser.objects.filter(
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
                "password_temporal": user._temporary_password,
                "nota": str(_('El usuario deberá cambiar esta contraseña en su primer inicio de sesión.'))
            },
            status=status.HTTP_201_CREATED
        )


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