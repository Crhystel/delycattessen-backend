from django.conf import settings
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from core.serializer_mixins import LineSummaryMixin
from core.timezone import local_today
from .models import PreOrder, PreOrderItem, Sale
from .services import GRANULARITIES, month_start_local


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


class SaleComboInputSerializer(serializers.Serializer):
    combo_id = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1)


class SaleCreateSerializer(serializers.Serializer):
    payment_method = serializers.ChoiceField(choices=Sale.PaymentMethod.choices)
    user_id = serializers.IntegerField(required=False)
    items = SaleItemInputSerializer(many=True, required=False, default=list)
    combos = SaleComboInputSerializer(many=True, required=False, default=list)

    def validate(self, attrs):
        if not attrs['items'] and not attrs['combos']:
            raise serializers.ValidationError({'items': _('Agrega al menos un producto o combo.')})
        return attrs


class SaleSerializer(LineSummaryMixin, serializers.ModelSerializer):
    class Meta:
        model = Sale
        fields = ('id', 'payment_method', 'total_amount', 'created_at', 'display_name', 'time', 'items')

    def get_lines(self, obj):
        lines = [(item.menu_item.name, item.quantity, item.price_at_purchase) for item in obj.items.all()]
        lines += [(combo.name, combo.quantity, combo.price_at_purchase) for combo in obj.combos.all()]
        return lines


class ParentSaleSerializer(SaleSerializer):
    """Point-of-sale purchase of one of the parent's children (parent history)."""
    student_id = serializers.IntegerField(source='customer.student_profile.id', read_only=True)
    student_name = serializers.SerializerMethodField()
    payment_method_display = serializers.CharField(source='get_payment_method_display', read_only=True)

    class Meta(SaleSerializer.Meta):
        fields = SaleSerializer.Meta.fields + ('student_id', 'student_name', 'payment_method_display')

    def get_student_name(self, obj):
        user = obj.customer
        return f"{user.first_name} {user.last_name}".strip() or user.username


class TeacherCreditRowSerializer(serializers.Serializer):
    """One teacher (in one institution) of the monthly credit report, with each sale."""
    teacher_id = serializers.IntegerField(source='teacher.id')
    full_name = serializers.CharField()
    email = serializers.EmailField(source='teacher.email', allow_null=True)
    institution_name = serializers.CharField(source='institution.name')
    consumptions = serializers.SerializerMethodField()
    total = serializers.DecimalField(max_digits=10, decimal_places=2)
    sales = SaleSerializer(many=True)

    def get_consumptions(self, row) -> int:
        return len(row['sales'])


class TeacherCreditTotalsSerializer(serializers.Serializer):
    teachers = serializers.IntegerField()
    consumptions = serializers.IntegerField()
    amount = serializers.DecimalField(max_digits=10, decimal_places=2)


class TeacherCreditReportSerializer(serializers.Serializer):
    """Credit consumptions of the current month until the moment it is generated."""
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    generated_at = serializers.DateTimeField()
    teachers = TeacherCreditRowSerializer(many=True)
    totals = TeacherCreditTotalsSerializer()


class AnalyticsQuerySerializer(serializers.Serializer):
    """Inclusive range of local dates for the sales analytics; defaults to the current month."""
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)

    def validate(self, attrs):
        attrs.setdefault('date_to', local_today())
        attrs.setdefault('date_from', min(month_start_local().date(), attrs['date_to']))
        if attrs['date_from'] > attrs['date_to']:
            raise serializers.ValidationError({'date_from': _('La fecha inicial no puede ser posterior a la final.')})
        days = (attrs['date_to'] - attrs['date_from']).days + 1
        if days > settings.ANALYTICS_MAX_RANGE_DAYS:
            raise serializers.ValidationError({
                'date_to': _('El rango no puede superar %(days)s días.') % {'days': settings.ANALYTICS_MAX_RANGE_DAYS},
            })
        return attrs


class AnalyticsTimeseriesQuerySerializer(AnalyticsQuerySerializer):
    granularity = serializers.ChoiceField(choices=GRANULARITIES, default='day')
