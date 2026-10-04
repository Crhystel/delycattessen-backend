from rest_framework import serializers
from .models import PreOrder, PreOrderItem

class PreOrderItemDetailSerializer(serializers.ModelSerializer):
    menu_item_name = serializers.CharField(source='menu_item.name', read_only=True)

    class Meta:
        model = PreOrderItem
        fields = ('id', 'menu_item', 'menu_item_name', 'quantity', 'price_at_purchase')


class PreOrderCreateSerializer(serializers.Serializer):
    student_id = serializers.IntegerField()
    items = serializers.ListField(
        child=serializers.DictField(),
        allow_empty=False
    )
class PreOrderListSerializer(serializers.ModelSerializer):
    items = PreOrderItemDetailSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    student_name = serializers.SerializerMethodField()

    class Meta:
        model = PreOrder
        fields = (
            'id', 'student', 'student_name', 'status', 'status_display',
            'total_amount', 'created_at', 'items',
        )

    def get_student_name(self, obj):
        user = obj.student.user
        full_name = f"{user.first_name} {user.last_name}".strip()
        return full_name or user.username
