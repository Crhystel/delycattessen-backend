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
    class Meta:
        model = Transaction
        fields = ['id', 'amount', 'gateway', 'status', 'type', 'created_at']
        read_only_fields = fields