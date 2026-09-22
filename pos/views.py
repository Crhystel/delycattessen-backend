from rest_framework import generics, status
from rest_framework.response import Response
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from .serializers import PreOrderCreateSerializer
from .models import PreOrder, PreOrderItem
from .mixins import AllergenValidatorMixin, ParentalControlValidatorMixin
from users.permissions import IsParentUser
from users.models import StudentProfile
from wallet.models import Wallet, Transaction
from catalog.models import MenuItem
from rest_framework.exceptions import ValidationError

class PreOrderCreateView(AllergenValidatorMixin, ParentalControlValidatorMixin, generics.CreateAPIView):
    serializer_class = PreOrderCreateSerializer
    permission_classes = [IsParentUser]

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        student_id = serializer.validated_data['student_id']
        items_data = serializer.validated_data['items']

        # Verificar que el estudiante pertenece al padre logueado
        try:
            student = StudentProfile.objects.get(id=student_id, parent__user=request.user)
        except StudentProfile.DoesNotExist:
            raise ValidationError(_('El estudiante no existe o no te pertenece.'))

        # Recuperar y bloquear la billetera (ACID)
        try:
            wallet = Wallet.objects.select_for_update().get(student=student)
        except Wallet.DoesNotExist:
            raise ValidationError(_('El estudiante no tiene una billetera configurada.'))

        menu_items = []
        total_amount = 0

        # Calcular totales y preparar items
        for item_data in items_data:
            item_id = item_data.get('menu_item_id')
            quantity = int(item_data.get('quantity', 1))
            
            menu_item = MenuItem.objects.get(id=item_id)
            if not menu_item.is_active:
                raise ValidationError(_('El producto "%s" no está disponible.') % menu_item.name)
            
            menu_items.append(menu_item)
            total_amount += menu_item.price * quantity

        # Validar alérgenos usando el Mixin
        self.validate_allergens(student, menu_items)

        # Validar saldo
        if wallet.balance < total_amount:
            raise ValidationError(_('Saldo insuficiente. Tienes $%(balance)s y el total es $%(total)s') % {
                'balance': wallet.balance, 'total': total_amount
            })

        # Descontar saldo y crear transacción
        wallet.balance -= total_amount
        wallet.save()

        Transaction.objects.create(
            wallet=wallet,
            amount=total_amount,
            status=Transaction.Status.SUCCESS,
            type=Transaction.Type.CONSUMPTION
        )

        # Crear PreOrden
        pre_order = PreOrder.objects.create(
            student=student,
            total_amount=total_amount,
            status=PreOrder.Status.PENDING
        )

        # Crear Items de la PreOrden
        for item_data in items_data:
            menu_item = MenuItem.objects.get(id=item_data.get('menu_item_id'))
            PreOrderItem.objects.create(
                pre_order=pre_order,
                menu_item=menu_item,
                quantity=int(item_data.get('quantity', 1)),
                price_at_purchase=menu_item.price
            )

        return Response(
            {"mensaje": _("Preorden creada exitosamente."), "pre_order_id": pre_order.id},
            status=status.HTTP_201_CREATED
        )


import jwt
from django.conf import settings
from django.core.cache import cache
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from users.permissions import IsOperativeUser
from users.models import CustomUser
from .mixins import BiometricValidationMixin

def _build_identified_user_payload(user: CustomUser, method: str) -> dict:
    """Constructs the standard user summary response for POS checkout."""
    balance = "0.00"
    allergies = []
    student_id = None

    if hasattr(user, 'student_profile'):
        student_profile = user.student_profile
        student_id = student_profile.id
        allergies = [a.name for a in student_profile.allergies.all()]
        if hasattr(student_profile, 'wallet'):
            balance = str(student_profile.wallet.balance)
    elif hasattr(user, 'wallet'):
        balance = str(user.wallet.balance)

    # Also check UserAllergy model
    user_allergies = [ua.allergen.name for ua in user.allergies.all()]
    all_allergies = sorted(list(set(allergies + user_allergies)))

    full_name = f"{user.first_name} {user.last_name}".strip()
    if not full_name:
        full_name = user.username

    return {
        "user_id": user.id,
        "student_id": student_id,
        "username": user.username,
        "full_name": full_name,
        "role": user.role,
        "institution": user.institution.name if user.institution else None,
        "balance": balance,
        "allergies": all_allergies,
        "identification_method": method,
    }


class POSFaceIdentificationView(BiometricValidationMixin, APIView):
    """
    POST /api/pos/identify/face/
    Receives camera frame from POS, extracts facial embedding in volatile memory,
    destroys raw image immediately, and matches against registered encrypted biometrics.
    Strictly protected for OPERATIONS_STAFF.
    """
    permission_classes = [IsOperativeUser]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, *args, **kwargs):
        photo = request.FILES.get('photo')
        if not photo:
            return Response(
                {"detail": _("Debe enviar una captura de rostro en el campo 'photo'.")},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 1. Extract embedding and wipe image memory
        try:
            candidate_embedding = self.extract_face_embedding(photo)
        except Exception as e:
            return Response(
                {"detail": f"Error al procesar rasgos faciales: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 2. Match against encrypted vectors in database
        matched_user, similarity = self.match_face(candidate_embedding, threshold=0.75)

        if not matched_user:
            return Response(
                {
                    "detail": _("No se pudo identificar al usuario por biometría facial."),
                    "suggestion": _("Por favor, utilice la vista de contingencia con código QR.")
                },
                status=status.HTTP_404_NOT_FOUND
            )

        return Response(
            _build_identified_user_payload(matched_user, method="FACE_RECOGNITION"),
            status=status.HTTP_200_OK
        )


class POSQrIdentificationView(APIView):
    """
    POST /api/pos/identify/qr/
    Receives dynamic QR token from POS scanner, validates HMAC-SHA256 signature,
    expiration (TTL 60s), and single-use nonce to prevent replay attacks.
    Strictly protected for OPERATIONS_STAFF.
    """
    permission_classes = [IsOperativeUser]
    parser_classes = [JSONParser]

    def post(self, request, *args, **kwargs):
        token = request.data.get('token')
        if not token:
            return Response(
                {"detail": _("El campo 'token' es obligatorio.")},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 1. Validate JWT signature and expiration
        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=['HS256'])
        except jwt.ExpiredSignatureError:
            return Response(
                {"detail": _("El código QR ha expirado. El estudiante debe refrescar su pantalla.")},
                status=status.HTTP_400_BAD_REQUEST
            )
        except jwt.InvalidTokenError:
            return Response(
                {"detail": _("Código QR inválido o alterado.")},
                status=status.HTTP_400_BAD_REQUEST
            )

        if payload.get('type') != 'pos_dynamic_qr':
            return Response(
                {"detail": _("Tipo de token no válido para identificación POS.")},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 2. Check and invalidate single-use nonce
        nonce = payload.get('nonce')
        if not nonce:
            return Response(
                {"detail": _("Token sin identificador único.")},
                status=status.HTTP_400_BAD_REQUEST
            )

        cache_key = f"used_qr_nonce_{nonce}"
        if cache.get(cache_key):
            return Response(
                {"detail": _("Este código QR ya fue utilizado. No se permite suplantación.")},
                status=status.HTTP_400_BAD_REQUEST
            )
        # Mark nonce as used for 5 minutes
        cache.set(cache_key, True, timeout=300)

        # 3. Retrieve user
        user_id = payload.get('user_id')
        try:
            user = CustomUser.objects.select_related('institution').get(pk=user_id, is_active=True)
        except CustomUser.DoesNotExist:
            return Response(
                {"detail": _("Usuario no encontrado o inactivo.")},
                status=status.HTTP_404_NOT_FOUND
            )

        return Response(
            _build_identified_user_payload(user, method="DYNAMIC_QR"),
            status=status.HTTP_200_OK
        )

