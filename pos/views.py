import logging

from django.utils.translation import gettext_lazy as _
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.generics import get_object_or_404
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.biometrics import BiometricValidationMixin
from core.mixins import resolve_institution_scope, resolve_write_institution
from core.timezone import local_now
from users.models import CustomUser
from users.permissions import IsOperativeUser, IsParentUser, IsTeacherUser, SameInstitutionAsTarget
from users.services import QrTokenExpired, QrTokenInvalid, decode_qr_token

from .models import PreOrder, Sale
from .serializers import (
    ParentSaleSerializer, PreOrderCreateSerializer, PreOrderListSerializer, SaleCreateSerializer, SaleSerializer,
)
from .services import (
    PreOrderCreator,
    build_identified_user_payload,
    consume_qr_nonce,
    deliver_preorder,
    monthly_credit_total,
    process_sale,
)

logger = logging.getLogger(__name__)


class PreOrderCreateView(generics.CreateAPIView):
    serializer_class = PreOrderCreateSerializer
    permission_classes = [IsParentUser]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        pre_order = PreOrderCreator(getattr(request.user, 'parent_profile', None)).create(
            data['student_id'], data['delivery_date'], data['items'],
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
        except ValidationError:
            raise
        except Exception:
            logger.exception("[BIOMETRIC] Error extracting facial features at the POS")
            return Response(
                {"detail": _("No se pudieron procesar los rasgos faciales de la imagen.")},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 2. Match against encrypted vectors in database (filtered by operative staff institution)
        institution = resolve_institution_scope(request)
        matched_user, similarity = self.match_face(
            candidate_embedding,
            institution=institution,
            all_institutions=institution is None,
        )

        if not matched_user:
            return Response(
                {
                    "detail": _("No se pudo identificar al usuario por biometría facial."),
                    "suggestion": _("Por favor, utilice la vista de contingencia con código QR.")
                },
                status=status.HTTP_404_NOT_FOUND
            )

        return Response(
            build_identified_user_payload(matched_user, method="FACE_RECOGNITION"),
            status=status.HTTP_200_OK
        )


class POSQrIdentificationView(APIView):
    """
    POST /api/pos/identify/qr/
    Receives dynamic QR token from POS scanner, validates HMAC-SHA256 signature,
    expiration (TTL 300s), and single-use nonce to prevent replay attacks.
    Strictly protected for OPERATIONS_STAFF.
    """
    permission_classes = [IsOperativeUser, SameInstitutionAsTarget]
    parser_classes = [JSONParser]

    def post(self, request, *args, **kwargs):
        token = request.data.get('token')
        if not token:
            return Response(
                {"detail": _("El campo 'token' es obligatorio.")},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            payload = decode_qr_token(token)
        except QrTokenExpired:
            return Response(
                {"detail": _("El código QR ha expirado. El estudiante debe refrescar su pantalla.")},
                status=status.HTTP_400_BAD_REQUEST
            )
        except QrTokenInvalid:
            return Response(
                {"detail": _("Código QR inválido o alterado.")},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Single-use: a nonce already consumed means the code was replayed.
        nonce = payload.get('nonce')
        if not nonce:
            return Response(
                {"detail": _("Token sin identificador único.")},
                status=status.HTTP_400_BAD_REQUEST
            )
        if not consume_qr_nonce(nonce):
            return Response(
                {"detail": _("Este código QR ya fue utilizado. No se permite suplantación.")},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Retrieve user
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

        self.check_object_permissions(request, user)
        return Response(
            build_identified_user_payload(user, method="DYNAMIC_QR"),
            status=status.HTTP_200_OK
        )

class PreOrderDeliverView(APIView):
    """
    PATCH /api/pos/preorders/<int:pre_order_id>/deliver/
    Marca como entregada una preorden pendiente, confirmada por el personal
    operativo tras identificar al estudiante en el POS. Solo puede
    entregarse una preorden cuya fecha de entrega sea hoy y esté en estado PENDING.
    """
    permission_classes = [IsOperativeUser, SameInstitutionAsTarget]

    def patch(self, request, pre_order_id):
        pre_order = get_object_or_404(PreOrder.objects.select_related('daily_menu', 'student'), pk=pre_order_id)
        self.check_object_permissions(request, pre_order)
        deliver_preorder(pre_order)
        return Response(
            {
                "detail": _("Pedido marcado como entregado."),
                "pre_order_id": pre_order.id,
                "status": pre_order.status,
            },
            status=status.HTTP_200_OK,
        )

class SaleCreateView(APIView):
    """
    POST /api/pos/sales/
    Charges products of the operator's institution. Without user_id it is a
    quick sale (cash only); with user_id the payment method decides who can pay.
    """
    permission_classes = [IsOperativeUser]

    def post(self, request):
        serializer = SaleCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        customer = None
        if 'user_id' in data:
            customer = get_object_or_404(
                CustomUser.objects.select_related('student_profile'), pk=data['user_id'],
            )

        sale = process_sale(
            data['payment_method'],
            resolve_write_institution(request),
            request.user,
            customer,
            [(item['menu_item_id'], item['quantity']) for item in data['items']],
        )
        return Response(SaleSerializer(sale).data, status=status.HTTP_201_CREATED)


class CreditHistoryView(APIView):
    """
    GET /api/pos/credit/history/
    Credit purchases of the authenticated teacher and what has accumulated
    since the first day of the current month (America/Guayaquil).
    """
    permission_classes = [IsAuthenticated, IsTeacherUser]
    max_results = 200

    def get(self, request):
        sales = Sale.objects.filter(
            customer=request.user, payment_method=Sale.PaymentMethod.CREDIT,
        ).prefetch_related('items__menu_item')
        return Response({
            'month': local_now().strftime('%Y-%m'),
            'accumulated_total': str(monthly_credit_total(request.user)),
            'results': SaleSerializer(sales[:self.max_results], many=True).data,
        })


class ParentSaleListView(generics.ListAPIView):
    """
    GET /api/pos/sales/history/?student_id=<id>  (student_id is optional)
    Point-of-sale purchases (cash, credit or wallet) of the authenticated
    parent's children, newest first. With student_id it narrows to that child
    (still limited to the parent's own children).
    """
    serializer_class = ParentSaleSerializer
    permission_classes = [IsParentUser]
    pagination_class = None
    max_results = 200

    def get_queryset(self):
        queryset = Sale.objects.filter(
            customer__student_profile__parent=self.request.user.parent_profile,
        ).select_related('customer__student_profile').prefetch_related('items__menu_item')

        student_id = self.request.query_params.get('student_id')
        if student_id:
            queryset = queryset.filter(customer__student_profile__id=student_id)

        return queryset.order_by('-created_at')[:self.max_results]
