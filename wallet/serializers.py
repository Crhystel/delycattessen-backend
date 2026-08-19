from decimal import Decimal

from rest_framework import serializers

from .models import Transaction, Wallet


class RechargeRequestSerializer(serializers.Serializer):
    wallet_id = serializers.IntegerField()
    amount = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=Decimal('0.01'))
    kushki_token = serializers.CharField(required=False, allow_blank=True)
    document_type = serializers.CharField(required=False, allow_blank=True)
    document_number = serializers.CharField(required=False, allow_blank=True)
    phone_number = serializers.CharField(required=False, allow_blank=True)

    def validate_wallet_id(self, value):
        if not Wallet.objects.filter(pk=value).exists():
            raise serializers.ValidationError('Wallet not found.')
        return value
    
class TransactionSerializer(serializers.ModelSerializer):
    display_name = serializers.SerializerMethodField()
    time = serializers.SerializerMethodField()

    class Meta:
        model = Transaction
        fields = ['id', 'display_name', 'amount', 'gateway', 'status', 'type', 'time', 'created_at']
        read_only_fields = fields

    def get_display_name(self, obj):
        if obj.type == Transaction.Type.RECHARGE:
            return 'Recarga de saldo'
        # TODO: once wallet is connected to the catalog app, replace this
        # with the actual product name from the consumption record.
        return 'Consumo'

    def get_time(self, obj):
        import zoneinfo
        local_time = obj.created_at.astimezone(zoneinfo.ZoneInfo('America/Guayaquil'))
        return local_time.strftime('%I:%M %p').lstrip('0').lower()