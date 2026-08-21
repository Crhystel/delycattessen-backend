from rest_framework import generics, status
from rest_framework.response import Response
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from .serializers import PreOrderCreateSerializer
from .models import PreOrder, PreOrderItem
from .mixins import AllergenValidatorMixin
from users.permissions import IsParentUser
from users.models import StudentProfile
from wallet.models import Wallet, Transaction
from catalog.models import MenuItem
from rest_framework.exceptions import ValidationError

class PreOrderCreateView(AllergenValidatorMixin, generics.CreateAPIView):
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
