from decimal import Decimal

from rest_framework import serializers

from .models import Transaction, Wallet


class RechargeRequestSerializer(serializers.Serializer):
    wallet_id = serializers.IntegerField()
    amount = serializers.DecimalField(
        max_digits=8,
        decimal_places=2,
        min_value=Decimal('0.01'),
    )

    def validate_wallet_id(self, value):
        if not Wallet.objects.filter(pk=value).exists():
            raise serializers.ValidationError('Wallet not found.')
        return value


class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = ['id', 'amount', 'gateway', 'status', 'type', 'created_at']
        read_only_fields = fields