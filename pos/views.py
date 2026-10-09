from decimal import Decimal
import zoneinfo
from datetime import datetime
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
from catalog.models import MenuDish, DailyMenu, MonthlyMenu
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
        delivery_date = validated_data['delivery_date']
        items_data = validated_data['items']

        try:
            student = StudentProfile.objects.select_related('institution', 'wallet', 'parent').get(id=student_id)
        except StudentProfile.DoesNotExist:
            raise ValidationError({'student_id': _('Estudiante no encontrado.')})

        # 1. El estudiante debe ser hijo del padre autenticado (si no -> 403)
        parent_profile = getattr(request.user, 'parent_profile', None)
        if not parent_profile or student.parent_id != parent_profile.id:
            return Response(
                {"detail": _("No tienes permiso sobre este estudiante.")},
                status=status.HTTP_403_FORBIDDEN
            )

        # 2. Hora de corte: delivery_date > today en America/Guayaquil
        today_ec = datetime.now(zoneinfo.ZoneInfo('America/Guayaquil')).date()
        if delivery_date <= today_ec:
            raise ValidationError(
                _("No se puede preordenar para hoy o fechas pasadas. La fecha de entrega debe ser posterior a hoy.")
            )

        # 3. DailyMenu de un MonthlyMenu PUBLICADO de la institución del estudiante
        try:
            daily_menu = DailyMenu.objects.select_related('monthly_menu').get(
                date=delivery_date,
                monthly_menu__institution=student.institution,
                monthly_menu__status=MonthlyMenu.Status.PUBLISHED,
            )
        except DailyMenu.DoesNotExist:
            raise ValidationError(
                _("No hay un menú publicado para esa fecha en la institución del estudiante.")
            )

        # 4. Platillos disponibles en el menú de esa fecha
        available_dishes = {dish.id: dish for dish in daily_menu.dishes.filter(is_active=True)}
        order_lines = []
        total_amount = Decimal('0.00')

        for item_data in items_data:
            dish_id = item_data['dish_id']
            quantity = item_data['quantity']

            if dish_id not in available_dishes:
                raise ValidationError(
                    _("El platillo solicitado no está disponible en el menú de esa fecha.")
                )

            dish = available_dishes[dish_id]
            order_lines.append((dish, quantity))
            total_amount += dish.price * quantity

        # 5. Control parental sobre la fecha de entrega
        self.validate_parental_controls(student, total_amount, target_date=delivery_date)

        # 6. Alérgenos
        self.validate_allergens(student, [dish for dish, _quantity in order_lines])

        # 7. Saldo en billetera
        wallet = student.wallet
        wallet = Wallet.objects.select_for_update().get(id=wallet.id)
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
            daily_menu=daily_menu,
            total_amount=total_amount,
            status=PreOrder.Status.PENDING,
        )

        for dish, quantity in order_lines:
            PreOrderItem.objects.create(
                pre_order=pre_order,
                dish=dish,
                quantity=quantity,
                price_at_purchase=dish.price,
            )

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
        ).select_related('student__user', 'daily_menu').prefetch_related('items__dish')

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

        today_ec = datetime.now(zoneinfo.ZoneInfo('America/Guayaquil')).date()
        pending_preorders = PreOrder.objects.filter(
            student=student_profile,
            status=PreOrder.Status.PENDING,
            daily_menu__date=today_ec,
        ).select_related('daily_menu').prefetch_related('items__dish').order_by('created_at')

        pending_orders = [
            {
                "pre_order_id": pre_order.id,
                "total_amount": str(pre_order.total_amount),
                "created_at": pre_order.created_at.isoformat(),
                "delivery_date": str(pre_order.delivery_date),
                "items": [
                    {
                        "dish_name": item.dish.name,
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
    expiration (TTL 300s), and single-use nonce to prevent replay attacks.
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
    entregarse una preorden cuya fecha de entrega sea hoy y esté en estado PENDING.
    """
    permission_classes = [IsOperativeUser]

    def patch(self, request, pre_order_id):
        pre_order = get_object_or_404(PreOrder.objects.select_related('daily_menu'), pk=pre_order_id)
        if pre_order.status != PreOrder.Status.PENDING:
            return Response(
                {"detail": _("Esta preorden no está pendiente de entrega.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        today_ec = datetime.now(zoneinfo.ZoneInfo('America/Guayaquil')).date()
        if pre_order.daily_menu.date != today_ec:
            formatted_date = pre_order.daily_menu.date.strftime('%d/%m/%Y')
            return Response(
                {"detail": _(f"Esta preorden es para el {formatted_date}, no para hoy.")},
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