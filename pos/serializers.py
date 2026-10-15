from rest_framework import serializers

from delycattessen_backend.serializer_mixins import LineSummaryMixin
from .models import PreOrder, PreOrderItem, Sale


class PreOrderItemDetailSerializer(serializers.ModelSerializer):
    dish_name = serializers.CharField(source='dish.name', read_only=True)

    class Meta:
        model = PreOrderItem
        fields = ('id', 'dish', 'dish_name', 'quantity', 'price_at_purchase')


class PreOrderItemInputSerializer(serializers.Serializer):
    dish_id = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1)


class PreOrderCreateSerializer(serializers.Serializer):
    student_id = serializers.IntegerField()
    delivery_date = serializers.DateField()
    items = PreOrderItemInputSerializer(many=True, allow_empty=False)


class PreOrderListSerializer(serializers.ModelSerializer):
    items = PreOrderItemDetailSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    student_name = serializers.SerializerMethodField()
    delivery_date = serializers.DateField(read_only=True)

    class Meta:
        model = PreOrder
        fields = (
            'id', 'student', 'student_name', 'delivery_date', 'status', 'status_display',
            'total_amount', 'created_at', 'items',
        )

    def get_student_name(self, obj):
        user = obj.student.user
        full_name = f"{user.first_name} {user.last_name}".strip()
        return full_name or user.username


class SaleItemInputSerializer(serializers.Serializer):
    menu_item_id = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1)


class SaleCreateSerializer(serializers.Serializer):
    payment_method = serializers.ChoiceField(choices=Sale.PaymentMethod.choices)
    user_id = serializers.IntegerField(required=False)
    items = SaleItemInputSerializer(many=True, allow_empty=False)


class SaleSerializer(LineSummaryMixin, serializers.ModelSerializer):
    class Meta:
        model = Sale
        fields = ('id', 'payment_method', 'total_amount', 'created_at', 'display_name', 'time', 'items')

    def get_lines(self, obj):
        return [(item.menu_item.name, item.quantity, item.price_at_purchase) for item in obj.items.all()]
