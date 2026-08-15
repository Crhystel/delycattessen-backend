from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.views import APIView
from rest_framework.viewsets import ModelViewSet
from django.contrib.auth import get_user_model
from django.utils.translation import gettext_lazy as _
from .serializers import (
    ChildSerializer,
    ParentRegistrationSerializer,
    StudentRegistrationSerializer,
    CreateStaffSerializer,
    PasswordResetRequestSerializer,
    PasswordResetConfirmSerializer,
    InstitutionSerializer,
    MeSerializer,
)
from .permissions import IsAdministrator, IsParentUser, SameInstitutionPermission, CanRequestPasswordReset
from .mixins import InstitutionScopeMixin, TokenGeneratorMixin
from .models import CustomUser, Institution
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.tokens import RefreshToken
from .serializers import EmailTokenObtainSerializer


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
                "message": str(_('%(role)s creado exitosamente.')) % {'role': user.get_role_display()},
                "email": user.email,
                "institution": user.institution.name,
                "temporary_password": user._temporary_password,
                "note": str(_('El usuario deberá cambiar esta contraseña en su primer inicio de sesión.'))
            },
            status=status.HTTP_201_CREATED
        )
class InstitutionListView(generics.ListAPIView):
    serializer_class = InstitutionSerializer
    permission_classes = [IsAuthenticated]
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
            {"message": str(self.success_message)},
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
    
class MeView(generics.RetrieveAPIView):
    serializer_class = MeSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user
    
class ParentRegistrationView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ParentRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        refresh = RefreshToken.for_user(user)
        return Response(
            {'access': str(refresh.access_token), 'refresh': str(refresh)},
            status=201,
        )


class StudentRegistrationView(APIView):
    permission_classes = [IsAuthenticated, IsParentUser]
    parser_classes = [MultiPartParser, FormParser]  

    def post(self, request):
        serializer = StudentRegistrationSerializer(
            data=request.data,
            context={'parent_profile': request.user.parent_profile},
        )
        serializer.is_valid(raise_exception=True)
        student_profile = serializer.save()

        return Response(
            {'student_id': student_profile.id, 'username': student_profile.user.username},
            status=201,
        )
class ChildrenListView(generics.ListAPIView):

    serializer_class = ChildSerializer
    permission_classes = [IsAuthenticated, IsParentUser]

    def get_queryset(self):
        return self.request.user.parent_profile.children.all()