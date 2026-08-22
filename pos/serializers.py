from rest_framework import serializers
from .models import PreOrder, PreOrderItem
from catalog.models import MenuItem

class PreOrderItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = PreOrderItem
        fields = ['menu_item', 'quantity', 'price_at_purchase']
        read_only_fields = ['price_at_purchase']

class PreOrderCreateSerializer(serializers.Serializer):
    student_id = serializers.IntegerField()
    items = serializers.ListField(
        child=serializers.DictField(),
        allow_empty=False
    )
