from rest_framework import generics, status
from rest_framework.response import Response
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from .serializers import PreOrderCreateSerializer, PreOrderListSerializer
from .models import PreOrder, PreOrderItem
from .mixins import AllergenValidatorMixin, ParentalControlValidatorMixin
from users.permissions import IsParentUser
from users.models import StudentProfile
from wallet.models import Wallet, Transaction
from catalog.models import MenuItem
from rest_framework.exceptions import ValidationError
from rest_framework.generics import get_object_or_404

class PreOrderCreateView(AllergenValidatorMixin, ParentalControlValidatorMixin, generics.CreateAPIView):
    serializer_class = PreOrderCreateSerializer
    permission_classes = [IsParentUser]

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated_data = serializer.validated_data

        student_id = validated_data['student_id']
        items_data = validated_data['items']

        student = StudentProfile.objects.select_related('wallet').get(id=student_id)
        wallet = student.wallet

        # (menu_item, quantity) por cada línea del pedido. select_for_update
        # bloquea la fila hasta que termine la transacción, para que dos
        # ventas simultáneas del mismo producto no dejen el stock negativo.
        order_lines = []
        total_amount = 0
        for item_data in items_data:
            item_id = item_data.get('menu_item_id')
            quantity = int(item_data.get('quantity', 1))
            menu_item = MenuItem.objects.select_for_update().get(id=item_id)

            if not menu_item.is_active:
                raise ValidationError(_('El producto "%s" no está disponible.') % menu_item.name)

            if menu_item.stock < quantity:
                raise ValidationError(
                    _('No hay suficiente stock de "%(product)s". Disponible: %(stock)s, solicitado: %(quantity)s') % {
                        'product': menu_item.name,
                        'stock': menu_item.stock,
                        'quantity': quantity,
                    }
                )

            order_lines.append((menu_item, quantity))
            total_amount += menu_item.price * quantity

        self.validate_allergens(student, [menu_item for menu_item, _quantity in order_lines])

        if wallet.balance < total_amount:
            raise ValidationError(
                _('Saldo insuficiente. Tienes $%(balance)s y el total es $%(total)s') % {
                    'balance': wallet.balance,
                    'total': total_amount,
                }
            )

        wallet.balance -= total_amount
        wallet.save()

        pre_order = PreOrder.objects.create(
            student=student,
            total_amount=total_amount,
            status=PreOrder.Status.PENDING,
        )

        for menu_item, quantity in order_lines:
            PreOrderItem.objects.create(
                pre_order=pre_order,
                menu_item=menu_item,
                quantity=quantity,
                price_at_purchase=menu_item.price,
            )
            menu_item.stock -= quantity
            menu_item.save()

        Transaction.objects.create(
            wallet=wallet,
            amount=total_amount,
            status=Transaction.Status.SUCCESS,
            type=Transaction.Type.CONSUMPTION,
            pre_order=pre_order,
        )

        return Response(
            {"mensaje": _("Preorden creada exitosamente."), "pre_order_id": pre_order.id},
            status=status.HTTP_201_CREATED,
        )
class PreOrderListView(generics.ListAPIView):
    """
    GET /api/pos/preorders/?student_id=<id>  (student_id es opcional)
    Lista las preórdenes (pendientes, entregadas, canceladas) de los hijos
    del padre autenticado, más recientes primero. Si se pasa student_id,
    filtra solo ese hijo (igual queda restringido a los hijos del padre).
    """
    serializer_class = PreOrderListSerializer
    permission_classes = [IsParentUser]

    def get_queryset(self):
        queryset = PreOrder.objects.filter(
            student__parent=self.request.user.parent_profile
        ).select_related('student__user').prefetch_related('items__menu_item')

        student_id = self.request.query_params.get('student_id')
        if student_id:
            queryset = queryset.filter(student_id=student_id)

        return queryset.order_by('-created_at')


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
    institution = user.institution  # default: staff/teacher institution
    pending_orders = []

    if hasattr(user, 'student_profile'):
        student_profile = user.student_profile
        student_id = student_profile.id
        allergies = [a.name for a in student_profile.allergies.all()]
        # A student's institution lives on StudentProfile, not on the
        # CustomUser itself (that field is only populated for staff).
        institution = student_profile.institution
        if hasattr(student_profile, 'wallet'):
            balance = str(student_profile.wallet.balance)

        pending_preorders = PreOrder.objects.filter(
            student=student_profile, status=PreOrder.Status.PENDING
        ).prefetch_related('items__menu_item').order_by('created_at')

        pending_orders = [
            {
                "pre_order_id": pre_order.id,
                "total_amount": str(pre_order.total_amount),
                "created_at": pre_order.created_at.isoformat(),
                "items": [
                    {
                        "menu_item_name": item.menu_item.name,
                        "quantity": item.quantity,
                        "price_at_purchase": str(item.price_at_purchase),
                    }
                    for item in pre_order.items.all()
                ],
            }
            for pre_order in pending_preorders
        ]
    elif hasattr(user, 'wallet'):
        balance = str(user.wallet.balance)

    full_name = f"{user.first_name} {user.last_name}".strip()
    if not full_name:
        full_name = user.username

    return {
        "user_id": user.id,
        "student_id": student_id,
        "username": user.username,
        "full_name": full_name,
        "role": user.role,
        "institution": institution.name if institution else None,
        "balance": balance,
        "allergies": sorted(set(allergies)),
        "pending_orders": pending_orders,
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
        matched_user, similarity = self.match_face(candidate_embedding, threshold=0.40)
        print(f"[BIOMETRIC POS] Best candidate score: {similarity:.4f} (threshold: 0.40), Matched: {matched_user}")

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
        # Mark nonce as used for 6 minutes (safely covers 5-minute token TTL)
        cache.set(cache_key, True, timeout=360)

        # 3. Retrieve user
        user_id = payload.get('user_id')
        try:
            user = CustomUser.objects.select_related(
                'institution', 'student_profile', 'student_profile__institution', 'student_profile__wallet'
            ).get(pk=user_id, is_active=True)
        except CustomUser.DoesNotExist:
            return Response(
                {"detail": _("Usuario no encontrado o inactivo.")},
                status=status.HTTP_404_NOT_FOUND
            )

        return Response(
            _build_identified_user_payload(user, method="DYNAMIC_QR"),
            status=status.HTTP_200_OK
        )

class PreOrderDeliverView(APIView):
    """
    PATCH /api/pos/preorders/<int:pre_order_id>/deliver/
    Marca como entregada una preorden pendiente, confirmada por el personal
    operativo tras identificar al estudiante en el POS. Solo puede
    entregarse una preorden que esté en estado PENDING.
    """
    permission_classes = [IsOperativeUser]

    def patch(self, request, pre_order_id):
        pre_order = get_object_or_404(PreOrder, pk=pre_order_id)
        if pre_order.status != PreOrder.Status.PENDING:
            return Response(
                {"detail": _("Esta preorden no está pendiente de entrega.")},
                status=status.HTTP_400_BAD_REQUEST,
            )
        pre_order.status = PreOrder.Status.DELIVERED
        pre_order.save(update_fields=['status'])
        return Response(
            {
                "detail": _("Pedido marcado como entregado."),
                "pre_order_id": pre_order.id,
                "status": pre_order.status,
            },
            status=status.HTTP_200_OK,
        )