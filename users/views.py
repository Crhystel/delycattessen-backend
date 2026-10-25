import logging

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.generics import get_object_or_404
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import ModelViewSet
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from catalog.models import Allergen

from core.mixins import InstitutionScopeMixin

from .mixins import TokenGeneratorMixin
from .models import CustomUser, Institution, ParentalControl, StudentProfile
from .permissions import (
    CanEditOwnProfile,
    CanManageStaffAccount,
    CanRegisterBiometric,
    CanRequestPasswordReset,
    IsAdministrator,
    IsParentOfStudent,
    IsParentUser,
    IsStudentOrTeacherUser,
    IsTeacherUser,
    SameInstitutionPermission,
    get_object_institution_id,
)
from .serializers import (
    AllergenSerializer,
    ChangePasswordSerializer,
    ChildProfileUpdateSerializer,
    ChildSerializer,
    CreateStaffSerializer,
    EmailTokenObtainSerializer,
    InstitutionSerializer,
    MeSerializer,
    ParentalControlSerializer,
    ParentRegistrationSerializer,
    PasswordResetConfirmSerializer,
    ProfileUpdateSerializer,
    PasswordResetRequestSerializer,
    SetPaymentPinSerializer,
    StudentRegistrationSerializer,
    UpdateStaffSerializer,
    VerifyPaymentPinSerializer,
)
from .services import (
    build_qr_token,
    create_staff_account,
    extract_unique_embedding,
    register_biometric,
    soft_delete_account,
    store_embedding,
    wallet_balance_of,
)

logger = logging.getLogger(__name__)


class StaffViewSet(InstitutionScopeMixin, ModelViewSet):
    """
    The Administrator manages the staff accounts of their institution(s):
    - Operations staff: full management (create, edit, activate/deactivate, delete).
    - Teachers: created and (de)activated by the admin; the teacher owns the rest
      of the profile (see CanManageStaffAccount).
    Deleting is always a soft delete (see soft_delete_account).
    """
    serializer_class = CreateStaffSerializer
    permission_classes = [IsAuthenticated, IsAdministrator, SameInstitutionPermission, CanManageStaffAccount]
    queryset = CustomUser.objects.filter(
        role__in=[CustomUser.Role.TEACHER, CustomUser.Role.OPERATIONS_STAFF],
        deleted_at__isnull=True,
    )

    def get_serializer_class(self):
        if self.action in ('update', 'partial_update'):
            return UpdateStaffSerializer
        return super().get_serializer_class()

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user, temporary_password = create_staff_account(
            serializer.validated_data,
            admin=request.user,
            target_institution=serializer.context.get('institution'),
        )
        return Response(
            {
                "message": str(_('%(role)s creado exitosamente.')) % {'role': user.get_role_display()},
                "email": user.email,
                "institution": user.institution.name,
                "temporary_password": temporary_password,
                "note": str(_('El usuario deberá cambiar esta contraseña en su primer inicio de sesión.'))
            },
            status=status.HTTP_201_CREATED
        )

    def perform_destroy(self, instance):
        soft_delete_account(instance)
        
        
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
        user = get_user_model().objects.filter(email=email, is_active=True).first()
        if user is not None:
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
    
class ProfileUpdateView(generics.UpdateAPIView):
    """
    PATCH /api/users/me/profile/
    A parent or teacher edits their own names and email; answers with the
    same shape as GET /api/users/me/.
    """
    serializer_class = ProfileUpdateSerializer
    permission_classes = [IsAuthenticated, CanEditOwnProfile]
    http_method_names = ['patch']

    def get_object(self):
        return self.request.user

    def update(self, request, *args, **kwargs):
        super().update(request, *args, **kwargs)  # PATCH: partial update
        return Response(MeSerializer(self.request.user).data)


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

        biometric_registered = getattr(student_profile, 'biometric_registered', False)
        return Response(
            {
                'student_id': student_profile.id,
                'username': student_profile.user.username,
                'biometric_registered': biometric_registered,
            },
            status=201,
        )
class ChildrenListView(generics.ListAPIView):

    serializer_class = ChildSerializer
    permission_classes = [IsAuthenticated, IsParentUser]

    def get_queryset(self):
        return self.request.user.parent_profile.children.all()


class ChildProfileUpdateView(APIView):
    """
    PATCH /api/users/children/<student_id>/
    The parent edits a child's names and username.
    """
    permission_classes = [IsAuthenticated, IsParentOfStudent]

    def patch(self, request, student_id):
        student_profile = get_object_or_404(StudentProfile.objects.select_related('user'), pk=student_id)
        serializer = ChildProfileUpdateSerializer(student_profile.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(ChildSerializer(student_profile).data)


class ChildPhotoUpdateView(APIView):
    """
    PATCH /api/users/children/<student_id>/photo/
    Updates the profile picture of a child and regenerates/updates the facial biometric embedding.
    """
    permission_classes = [IsAuthenticated, IsParentOfStudent]
    parser_classes = [MultiPartParser, FormParser]

    def patch(self, request, student_id):
        student_profile = get_object_or_404(StudentProfile, pk=student_id)
        if 'profile_picture' not in request.FILES:
            return Response({'error': _('No se envió ninguna imagen.')}, status=status.HTTP_400_BAD_REQUEST)

        photo = request.FILES['profile_picture']

        # The new face must be valid and unique BEFORE anything is saved, so the
        # photo and the embedding used by the POS never get out of sync.
        vector = extract_unique_embedding(
            photo, student_profile.institution, exclude_user=student_profile.user,
        )

        with transaction.atomic():
            store_embedding(student_profile.user, vector)
            photo.seek(0)
            student_profile.profile_picture = photo
            student_profile.save()

        data = ChildSerializer(student_profile).data
        data['biometric_registered'] = True
        return Response(data, status=status.HTTP_200_OK)
    
class AllergenListView(generics.ListAPIView):
    """GET /api/users/allergens/ - allergen catalog"""
    queryset = Allergen.objects.all().order_by('name')
    serializer_class = AllergenSerializer
    permission_classes = [IsAuthenticated]
       
class BaseAllergyView(APIView):
    """
    Template Method: reads and replaces the allergies of an owner (a student
    profile or a teacher). Subclasses only decide who the owner is.
    """

    def get_owner(self, request, **kwargs):
        raise NotImplementedError

    def get(self, request, **kwargs):
        owner = self.get_owner(request, **kwargs)
        return Response(AllergenSerializer(owner.allergies.all(), many=True).data)

    def put(self, request, **kwargs):
        owner = self.get_owner(request, **kwargs)
        allergen_ids = request.data.get('allergen_ids', [])
        try:
            allergens = list(Allergen.objects.filter(pk__in=allergen_ids))
        except (TypeError, ValueError):
            allergens = []
        if len(allergens) != len(set(allergen_ids)):
            raise DRFValidationError({'allergen_ids': _('Uno o más alérgenos no existen.')})
        owner.allergies.set(allergens)

        # An allergen with no MenuItem tagged can't be blocked by the POS yet,
        # so the owner gets a heads-up instead of a false sense of protection.
        unprotected = [a.name for a in allergens if not a.menu_items.exists()]

        return Response({
            'allergies': AllergenSerializer(allergens, many=True).data,
            'unprotected_allergens': unprotected,
        })


class StudentAllergyView(BaseAllergyView):
    """GET/PUT /api/users/students/<student_id>/allergies/"""

    permission_classes = [IsAuthenticated, IsParentOfStudent]

    def get_owner(self, request, student_id):
        return get_object_or_404(StudentProfile, pk=student_id)


class TeacherAllergyView(BaseAllergyView):
    """GET/PUT /api/users/allergies/ - the authenticated teacher's own allergies"""

    permission_classes = [IsAuthenticated, IsTeacherUser]

    def get_owner(self, request):
        return request.user


class SetPaymentPinView(APIView):
    """POST /api/users/set-payment-pin/ — the dad creates or changes his PIN"""

    permission_classes = [IsAuthenticated, IsParentUser]

    def post(self, request):
        serializer = SetPaymentPinSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        request.user.parent_profile.set_payment_pin(serializer.validated_data['pin'])
        return Response({'detail': _('PIN actualizado correctamente.')})


class VerifyPaymentPinView(APIView):
    """POST /api/users/verify-payment-pin/ — It runs before confirming a 
    top-up (Kushki or Payphone), even if the JWT session is still active."""

    permission_classes = [IsAuthenticated, IsParentUser]

    def post(self, request):
        serializer = VerifyPaymentPinSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        is_valid = request.user.parent_profile.check_payment_pin(serializer.validated_data['pin'])
        return Response({'valid': is_valid})

class ParentalControlView(generics.RetrieveUpdateAPIView):
    serializer_class = ParentalControlSerializer
    permission_classes = [IsAuthenticated, IsParentUser]

    def get_object(self):
        student_id = self.kwargs.get('student_id')
        student = generics.get_object_or_404(StudentProfile, id=student_id, parent=self.request.user.parent_profile)
        obj, created = ParentalControl.objects.get_or_create(student=student)
        return obj


class DynamicQrTokenView(APIView):
    """
    GET /api/users/qr/token/
    Generates a dynamic, time-sensitive QR token (see QR_TOKEN_TTL_SECONDS)
    for the authenticated student or teacher, preventing spoofing with a single-use nonce.
    Also returns the user's fresh wallet balance and profile summary for the contingency view.
    """
    permission_classes = [IsAuthenticated, IsStudentOrTeacherUser]

    def get(self, request):
        user = request.user
        token, issued_at, ttl_seconds = build_qr_token(user)
        full_name = f"{user.first_name} {user.last_name}".strip() or user.username

        return Response({
            "token": token,
            "expires_in": ttl_seconds,
            "issued_at": issued_at.isoformat(),
            "user_id": user.id,
            "full_name": full_name,
            "balance": wallet_balance_of(user),
            "role": user.role,
        })


class RegisterBiometricView(APIView):
    """
    POST /api/users/biometrics/register/
    Registers or updates the biometric facial vector of a student (by their
    parent) or a teacher (by an admin of the same institution); see
    CanRegisterBiometric. No other role has biometrics.
    Strictly complies with Data Protection Laws:
    Extracts embedding vector, encrypts with AES-256-GCM, and destroys raw image in memory.
    """
    permission_classes = [IsAuthenticated, CanRegisterBiometric]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        photo = request.FILES.get('photo')
        if not photo:
            return Response(
                {"detail": _("Debe enviar una fotografía en el campo 'photo'.")},
                status=status.HTTP_400_BAD_REQUEST
            )

        target_user_id = request.data.get('target_user_id')
        if not target_user_id:
            return Response(
                {"detail": _("Debe indicar el usuario en el campo 'target_user_id'.")},
                status=status.HTTP_400_BAD_REQUEST
            )
        try:
            target_user = CustomUser.objects.select_related(
                'student_profile__parent'
            ).get(pk=target_user_id)
        except (CustomUser.DoesNotExist, ValueError):
            return Response({"detail": _("Usuario objetivo no encontrado.")}, status=status.HTTP_404_NOT_FOUND)

        # Template: a single object-level check decides who may register whom.
        self.check_object_permissions(request, target_user)

        # Extracts the vector (wiping the raw frame), checks the face is not
        # registered to someone else of the institution and stores it encrypted.
        register_biometric(target_user, photo, get_object_institution_id(target_user))

        return Response({
            "detail": _("Perfil biométrico facial registrado exitosamente con cifrado AES-256."),
            "user_id": target_user.id,
            "username": target_user.username,
        }, status=status.HTTP_201_CREATED)

class ChangePasswordView(BasePasswordResetView):
    """POST /api/users/change-password/ — el usuario autenticado define una
    contraseña propia. Se usa para el cambio obligatorio tras el primer
    login (must_change_password=True) y sirve como cambio de contraseña en
    general. Sigue el mismo Template Method que el resto del flujo de
    contraseñas (BasePasswordResetView.post ya valida y arma la respuesta;
    aquí solo se define el paso que varía)."""
    serializer_class = ChangePasswordSerializer
    permission_classes = [IsAuthenticated]
    success_message = _('Contraseña actualizada correctamente.')

    def process_recovery_action(self, serializer):
        serializer.save()