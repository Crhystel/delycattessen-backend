from decimal import Decimal

from django.conf import settings
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from delycattessen_backend.serializer_mixins import LineSummaryMixin

from .models import Transaction, Wallet


def validate_recharge_amount(value):
    """Single place that enforces the minimum recharge amount."""
    minimum = Decimal(str(settings.WALLET_RECHARGE_MIN_AMOUNT))
    if value < minimum:
        raise serializers.ValidationError(
            _('El monto mínimo de recarga es $%(minimum).2f.') % {'minimum': minimum}
        )
    return value


class RechargeGatewayQuerySerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=Decimal('0.01'))

    def validate_amount(self, value):
        return validate_recharge_amount(value)


class RechargeRequestSerializer(serializers.Serializer):
    wallet_id = serializers.IntegerField()
    amount = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=Decimal('0.01'))
    kushki_token = serializers.CharField(required=False, allow_blank=True)
    document_type = serializers.CharField(required=False, allow_blank=True)
    document_number = serializers.CharField(required=False, allow_blank=True)
    phone_number = serializers.CharField(required=False, allow_blank=True)

    def validate_amount(self, value):
        return validate_recharge_amount(value)

    def validate_wallet_id(self, value):
        if not Wallet.objects.filter(pk=value).exists():
            raise serializers.ValidationError(_('Billetera no encontrada.'))
        return value


class TransactionSerializer(LineSummaryMixin, serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = ['id', 'display_name', 'amount', 'gateway', 'status', 'type', 'time', 'created_at', 'items']
        read_only_fields = fields

    def get_lines(self, obj):
        if obj.sale_id:
            return [(item.menu_item.name, item.quantity, item.price_at_purchase) for item in obj.sale.items.all()]
        if obj.pre_order_id:
            return [
                (item.dish.name if item.dish else _('Producto'), item.quantity, item.price_at_purchase)
                for item in obj.pre_order.items.all()
            ]
        return []

    def get_display_name(self, obj):
        if obj.type == Transaction.Type.RECHARGE:
            return _('Recarga de saldo')
        return super().get_display_name(obj)
