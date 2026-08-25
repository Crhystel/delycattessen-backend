from rest_framework import generics, status
from rest_framework.generics import get_object_or_404
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
    AllergenSerializer,
    UserAllergySerializer,
    EmailTokenObtainSerializer,
    SetPaymentPinSerializer,
    VerifyPaymentPinSerializer,
)
from .permissions import IsAdministrator, IsParentUser, SameInstitutionPermission, CanRequestPasswordReset, CanManageAllergies, IsParentOfStudent
from .mixins import InstitutionScopeMixin, TokenGeneratorMixin
from .models import CustomUser, Institution, Allergen, UserAllergy, StudentProfile
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.tokens import RefreshToken


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
    
class AllergenListView(generics.ListAPIView):
    """GET /api/users/allergens/ - allergen catalog"""
    queryset = Allergen.objects.all().order_by('name')
    serializer_class = AllergenSerializer
    permission_classes = [IsAuthenticated]
    
class UserAllergyListView(APIView):
    """GET/PUT /api/users/allergies/?target_user_id=<id>
    PUT replaces complete list of alergies from user"""
    permission_classes = [IsAuthenticated, CanManageAllergies]

    def get(self, request):
        target_user_id = request.query_params.get('target_user_id')
        allergies = UserAllergy.objects.filter(user_id=target_user_id).select_related('allergen')
        return Response(UserAllergySerializer(allergies, many=True).data)

    def put(self, request):
        target_user_id = request.data.get('target_user_id')
        allergen_ids = request.data.get('allergen_ids', [])
        UserAllergy.objects.filter(user_id=target_user_id).delete()
        UserAllergy.objects.bulk_create([
            UserAllergy(user_id=target_user_id, allergen_id=aid) for aid in allergen_ids
        ])
        allergies = UserAllergy.objects.filter(user_id=target_user_id).select_related('allergen')
        return Response(UserAllergySerializer(allergies, many=True).data)
    
class StudentAllergyView(APIView):
    """GET/PUT /api/users/students/<student_id>/allergies/"""

    permission_classes = [IsAuthenticated, IsParentOfStudent]

    def get(self, request, student_id):
        student = get_object_or_404(StudentProfile, pk=student_id)
        return Response(AllergenSerializer(student.allergies.all(), many=True).data)

    def put(self, request, student_id):
        student = get_object_or_404(StudentProfile, pk=student_id)
        allergen_ids = request.data.get('allergen_ids', [])
        student.allergies.set(allergen_ids)
        return Response(AllergenSerializer(student.allergies.all(), many=True).data)

class SetPaymentPinView(APIView):
    """POST /api/users/set-payment-pin/ — the dad creates or changes his PIN"""

    permission_classes = [IsAuthenticated, IsParentUser]

    def post(self, request):
        serializer = SetPaymentPinSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        request.user.parent_profile.set_payment_pin(serializer.validated_data['pin'])
        return Response({'detail': 'PIN actualizado correctamente.'})


class VerifyPaymentPinView(APIView):
    """POST /api/users/verify-payment-pin/ — It runs before confirming a 
    top-up (Kushki or Payphone), even if the JWT session is still active."""

    permission_classes = [IsAuthenticated, IsParentUser]

    def post(self, request):
        serializer = VerifyPaymentPinSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        is_valid = request.user.parent_profile.check_payment_pin(serializer.validated_data['pin'])
        return Response({'valid': is_valid})