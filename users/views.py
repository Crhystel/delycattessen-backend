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
from .models import ParentalControl
from .serializers import ParentalControlSerializer

class ParentalControlView(generics.RetrieveUpdateAPIView):
    serializer_class = ParentalControlSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        student_id = self.kwargs.get('student_id')
        student = generics.get_object_or_404(StudentProfile, id=student_id, parent=self.request.user.parent_profile)
        obj, created = ParentalControl.objects.get_or_create(student=student)
        return obj


import jwt
import uuid
from datetime import datetime, timezone, timedelta
from django.conf import settings
from rest_framework.parsers import MultiPartParser, FormParser
from .models import UserBiometric
from .permissions import IsStudentOrTeacherUser
from pos.mixins import BiometricValidationMixin

class DynamicQrTokenView(APIView):
    """
    GET /api/users/qr/token/
    Generates a dynamic, time-sensitive QR token (valid for 60 seconds)
    for the authenticated student or teacher, preventing spoofing with a single-use nonce.
    Also returns the user's fresh wallet balance and profile summary for the contingency view.
    """
    permission_classes = [IsAuthenticated, IsStudentOrTeacherUser]

    def get(self, request):
        user = request.user
        now = datetime.now(timezone.utc)
        ttl_seconds = 60
        nonce = uuid.uuid4().hex

        payload = {
            'sub': str(user.id),
            'user_id': user.id,
            'username': user.username,
            'role': user.role,
            'nonce': nonce,
            'type': 'pos_dynamic_qr',
            'iat': now,
            'exp': now + timedelta(seconds=ttl_seconds),
        }
        token = jwt.encode(payload, settings.SECRET_KEY, algorithm='HS256')

        # Balance resolution
        balance = "0.00"
        if hasattr(user, 'student_profile') and hasattr(user.student_profile, 'wallet'):
            balance = str(user.student_profile.wallet.balance)
        elif hasattr(user, 'wallet'):
            balance = str(user.wallet.balance)

        full_name = f"{user.first_name} {user.last_name}".strip() or user.username

        return Response({
            "token": token,
            "expires_in": ttl_seconds,
            "issued_at": now.isoformat(),
            "user_id": user.id,
            "full_name": full_name,
            "balance": balance,
            "role": user.role,
        })


class RegisterBiometricView(BiometricValidationMixin, APIView):
    """
    POST /api/users/biometrics/register/
    Registers or updates the biometric facial vector of a student, teacher, or user.
    Strictly complies with Data Protection Laws:
    Extracts embedding vector, encrypts with AES-256-GCM, and destroys raw image in memory.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        photo = request.FILES.get('photo')
        if not photo:
            return Response(
                {"detail": _("Debe enviar una fotografía en el campo 'photo'.")},
                status=status.HTTP_400_BAD_REQUEST
            )

        target_user_id = request.data.get('target_user_id')
        if target_user_id:
            try:
                target_user = CustomUser.objects.get(pk=target_user_id)
            except CustomUser.DoesNotExist:
                return Response({"detail": _("Usuario objetivo no encontrado.")}, status=status.HTTP_404_NOT_FOUND)
        else:
            target_user = request.user

        # 1. Extract embedding vector (wipes raw frame memory)
        try:
            vector = self.extract_face_embedding(photo)
        except Exception as e:
            return Response({"detail": f"Error al extraer rasgos biométricos: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

        # 2. Encrypt vector with AES-256-GCM
        ciphertext, nonce, tag = self.encrypt_embedding(vector)

        # 3. Store only encrypted vector, nonce, tag
        UserBiometric.objects.update_or_create(
            user=target_user,
            defaults={
                'encrypted_embedding': ciphertext,
                'nonce': nonce,
                'tag': tag,
                'is_active': True,
            }
        )

        return Response({
            "detail": _("Perfil biométrico facial registrado exitosamente con cifrado AES-256."),
            "user_id": target_user.id,
            "username": target_user.username,
        }, status=status.HTTP_201_CREATED)

