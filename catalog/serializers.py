from decimal import Decimal
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from .models import MenuItem, Allergen, Ingredient, MenuDish, MonthlyMenu, DailyMenu, Promotion, Combo
from .mixins import IngredientValidationMixin, AllergenSyncMixin, ValidityPeriodSerializerMixin
from .services import ComboService, PromotionService


class AllergenSerializer(serializers.ModelSerializer):
    class Meta:
        model = Allergen
        fields = ['id', 'name', 'description']


class AllergenCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Allergen
        fields = ['id', 'name', 'description']
    def validate_name(self, value):
        if Allergen.objects.filter(name__iexact=value).exists():
            raise serializers.ValidationError(_('Ya existe un alérgeno con ese nombre.'))
        return value
    
class IngredientSerializer(serializers.ModelSerializer):
    """`allergens_reviewed` is read-only: it's flipped to True automatically
    whenever a request explicitly sets `allergens` (see update()), so an
    admin marking "sin alérgenos conocidos" with an empty list still counts
    as a deliberate review — never silently left unreviewed."""

    class Meta:
        model = Ingredient
        fields = ['id', 'name', 'description', 'allergens', 'allergens_reviewed']
        read_only_fields = ['allergens_reviewed']

    def update(self, instance, validated_data):
        if 'allergens' in self.initial_data:
            validated_data['allergens_reviewed'] = True
        return super().update(instance, validated_data)


class IngredientCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ingredient
        fields = ['id', 'name', 'description', 'allergens', 'allergens_reviewed']
        read_only_fields = ['allergens_reviewed']

    def validate_name(self, value):
        if Ingredient.objects.filter(name__iexact=value).exists():
            raise serializers.ValidationError(_('Ya existe un ingrediente con ese nombre.'))
        return value

    def create(self, validated_data):
        if 'allergens' in self.initial_data:
            validated_data['allergens_reviewed'] = True
        return super().create(validated_data)


class MenuItemSerializer(IngredientValidationMixin, AllergenSyncMixin, serializers.ModelSerializer):
    """`ingredients` is writable as a list of Ingredient ids. `allergens` is
    NEVER set directly by the client — it's auto-computed as the union of
    the selected ingredients' allergens, so allergen safety never depends
    on an admin remembering to tag it manually. On read, both fields
    return nested objects (id + name), not bare ids.

    is_active/is_visible are declared explicitly (instead of relying on
    ModelSerializer's automatic introspection) because DRF's default
    handling for BooleanField can be unreliable on multipart/form-data
    requests — without this, a new product created with an image upload
    could come back with both False despite the model's default=True.

    `price` is always the original price; `discount_percentage` and
    `final_price` reflect the promotion in force today (see PromotionService),
    so they go back to null/`price` on their own once it expires."""

    is_active = serializers.BooleanField(default=True, required=False)
    is_visible = serializers.BooleanField(default=True, required=False)
    institution_name = serializers.CharField(source='institution.name', read_only=True)

    class Meta:
        model = MenuItem
        fields = [
            'id', 'name', 'description', 'category', 'image', 'price',
            'is_active', 'is_visible', 'stock', 'ingredients', 'allergens',
            'institution_name',
        ]
        read_only_fields = ['allergens']

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['ingredients'] = IngredientSerializer(instance.ingredients.all(), many=True).data
        data['allergens'] = AllergenSerializer(instance.allergens.all(), many=True).data
        discount = self._active_discount(instance)
        data['discount_percentage'] = discount
        data['final_price'] = str(PromotionService.discounted_price(instance.price, discount))
        return data

    @staticmethod
    def _active_discount(instance):
        # List views prefetch today's promotions into `promotions_in_force`
        # (see PromotionsInForceMixin) to avoid one query per product.
        promotions = getattr(instance, 'promotions_in_force', None)
        if promotions is None:
            promotions = instance.promotions.in_force(PromotionService.today())
        promotion = next(iter(promotions), None)
        return promotion.discount_percentage if promotion else None


class MenuDishSerializer(IngredientValidationMixin, AllergenSyncMixin, serializers.ModelSerializer):
    """
    Serializer for monthly menu dishes.
    - ingredients is writable as a list of Ingredient IDs and represented nested on read.
    - allergens is read-only, calculated automatically via AllergenSyncMixin.
    - is_active is declared explicitly for multipart/form-data robustness.
    - institution is assigned by the view (the admin's write target).
    """
    is_active = serializers.BooleanField(default=True, required=False)
    price = serializers.DecimalField(max_digits=6, decimal_places=2, min_value=Decimal('0.01'))
    institution_name = serializers.CharField(source='institution.name', read_only=True)

    class Meta:
        model = MenuDish
        fields = [
            'id', 'name', 'description', 'price', 'image',
            'ingredients', 'allergens', 'is_active', 'institution_name',
        ]
        read_only_fields = ['allergens']

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['ingredients'] = IngredientSerializer(instance.ingredients.all(), many=True).data
        data['allergens'] = AllergenSerializer(instance.allergens.all(), many=True).data
        return data


class MonthlyMenuCreateSerializer(serializers.ModelSerializer):
    """
    Validates creation of a MonthlyMenu for the target institution (context['institution']).
    """
    year = serializers.IntegerField(min_value=2000, max_value=2100)
    month = serializers.IntegerField(min_value=1, max_value=12)

    class Meta:
        model = MonthlyMenu
        fields = ['year', 'month']

    def validate(self, attrs):
        institution = self.context.get('institution')
        if not institution:
            raise serializers.ValidationError(_('El usuario no tiene una institución asignada.'))

        year = attrs.get('year')
        month = attrs.get('month')
        if MonthlyMenu.objects.filter(institution=institution, year=year, month=month).exists():
            raise serializers.ValidationError(_('Ya existe un menú mensual para ese mes.'))
        return attrs


class DailyMenuSerializer(serializers.ModelSerializer):
    """
    Serializes a business day in the monthly menu planning.
    Includes associated dish IDs and count of pre-orders placed.
    """
    dish_ids = serializers.PrimaryKeyRelatedField(source='dishes', many=True, read_only=True)
    preorder_count = serializers.SerializerMethodField()

    class Meta:
        model = DailyMenu
        fields = ['id', 'date', 'dish_ids', 'preorder_count']

    def get_preorder_count(self, obj) -> int:
        return obj.pre_orders.count()


class MonthlyMenuSerializer(serializers.ModelSerializer):
    """
    Detailed serializer for MonthlyMenu including its days and aggregate preorders.
    """
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    days = DailyMenuSerializer(many=True, read_only=True)
    total_preorders = serializers.SerializerMethodField()
    institution_name = serializers.CharField(source='institution.name', read_only=True)

    class Meta:
        model = MonthlyMenu
        fields = [
            'id', 'year', 'month', 'status', 'status_display',
            'published_at', 'days', 'total_preorders', 'institution_name',
        ]

    def get_total_preorders(self, obj) -> int:
        from pos.models import PreOrder
        return PreOrder.objects.filter(daily_menu__monthly_menu=obj).count()


class DailyMenuUpdateSerializer(serializers.Serializer):
    """
    Validates assigning dishes to a specific daily menu.
    Requires dish_ids belonging to the target institution (context['institution']) and active.
    """
    dish_ids = serializers.ListField(
        child=serializers.IntegerField(),
        allow_empty=True,
    )

    def validate_dish_ids(self, value):
        institution = self.context.get('institution')
        if not institution:
            raise serializers.ValidationError(_('El usuario no tiene una institución asignada.'))

        dishes = list(MenuDish.objects.filter(id__in=value, institution=institution, is_active=True))
        if len(dishes) != len(set(value)):
            raise serializers.ValidationError(
                _('Uno o más platillos no existen, están inactivos o no pertenecen a su institución.')
            )
        return value


class StudentMenuDishSerializer(serializers.ModelSerializer):
    """
    Serializes a MenuDish for parent consultation with student-specific allergen detection
    and absolute image URLs.
    """
    price = serializers.DecimalField(max_digits=6, decimal_places=2)
    allergens = serializers.SerializerMethodField()
    has_student_allergens = serializers.SerializerMethodField()
    image = serializers.SerializerMethodField()

    class Meta:
        model = MenuDish
        fields = [
            'id', 'name', 'description', 'price', 'image',
            'allergens', 'has_student_allergens',
        ]

    def get_allergens(self, obj) -> list[str]:
        return [a.name for a in obj.allergens.all()]

    def get_has_student_allergens(self, obj) -> bool:
        student_allergy_ids = self.context.get('student_allergy_ids')
        if student_allergy_ids is None:
            student = self.context.get('student')
            if not student:
                return False
            student_allergy_ids = set(student.allergies.values_list('id', flat=True))
            self.context['student_allergy_ids'] = student_allergy_ids
        dish_allergy_ids = set(a.id for a in obj.allergens.all())
        return bool(student_allergy_ids & dish_allergy_ids)

    def get_image(self, obj) -> str | None:
        if not obj.image:
            return None
        request = self.context.get('request')
        if request:
            return request.build_absolute_uri(obj.image.url)
        return obj.image.url


class StudentDailyMenuSerializer(serializers.ModelSerializer):
    """
    Serializes a DailyMenu for a student's parent, evaluating parental control restrictions.
    """
    is_blocked_by_parental_control = serializers.SerializerMethodField()
    blocked_reason = serializers.SerializerMethodField()
    dishes = serializers.SerializerMethodField()

    class Meta:
        model = DailyMenu
        fields = [
            'id', 'date', 'is_blocked_by_parental_control',
            'blocked_reason', 'dishes',
        ]

    def _get_parental_control_info(self, obj):
        if hasattr(obj, '_parental_control_info'):
            return obj._parental_control_info

        student = self.context.get('student')
        is_blocked = False
        blocked_reason = None

        if student and hasattr(student, 'parental_control'):
            control = student.parental_control
            if control.allowed_days_enabled:
                if obj.date.weekday() not in control.allowed_days:
                    is_blocked = True
                    blocked_reason = "No está permitido comprar para ese día: está bloqueado por el control parental."

        obj._parental_control_info = (is_blocked, blocked_reason)
        return obj._parental_control_info

    def get_is_blocked_by_parental_control(self, obj) -> bool:
        return self._get_parental_control_info(obj)[0]

    def get_blocked_reason(self, obj) -> str | None:
        return self._get_parental_control_info(obj)[1]

    def get_dishes(self, obj):
        dishes = [d for d in obj.dishes.all() if d.is_active]
        return StudentMenuDishSerializer(dishes, many=True, context=self.context).data

class PromotionProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = MenuItem
        fields = ['id', 'name', 'price', 'image']


class PromotionSerializer(ValidityPeriodSerializerMixin, serializers.ModelSerializer):
    """
    `products` is writable as a list of MenuItem ids of the target institution
    (context['institution']) and returned nested on read. `status` is never
    stored: it is derived from today's date, so a promotion becomes "Vencida"
    by itself and its products go back to their original price.
    """
    products = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=MenuItem.objects.filter(is_active=True),
        allow_empty=False,
        error_messages={'empty': _('Selecciona al menos un producto para la promoción.')},
    )
    discount_percentage = serializers.IntegerField(
        min_value=1,
        max_value=99,
        error_messages={
            'min_value': _('El descuento debe estar entre 1%% y 99%%.'),
            'max_value': _('El descuento debe estar entre 1%% y 99%%.'),
        },
    )
    institution_name = serializers.CharField(source='institution.name', read_only=True)
    service = PromotionService

    class Meta:
        model = Promotion
        fields = [
            'id', 'products', 'discount_percentage', 'start_date', 'end_date',
            'image', 'status', 'institution_name',
        ]

    def validate_products(self, products):
        institution = self.context.get('institution')
        if institution and any(p.institution_id != institution.id for p in products):
            raise serializers.ValidationError(_('Uno o más productos no pertenecen a esta institución.'))
        return products

    def validate(self, attrs):
        instance = self.instance
        start_date, end_date = self.validate_period(attrs)
        products = attrs.get('products', instance.products.all() if instance else [])
        PromotionService.validate_products_available(
            products, start_date, end_date, exclude_id=getattr(instance, 'pk', None)
        )
        return attrs

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['products'] = PromotionProductSerializer(
            instance.products.all(), many=True, context=self.context
        ).data
        return data


class ComboItemSerializer(serializers.Serializer):
    """Product of a combo on read, with its own price to show the saving."""
    product_id = serializers.IntegerField(source='menu_item.id')
    name = serializers.CharField(source='menu_item.name')
    price = serializers.DecimalField(source='menu_item.price', max_digits=6, decimal_places=2)
    quantity = serializers.IntegerField()


class ComboItemInputSerializer(serializers.Serializer):
    product_id = serializers.PrimaryKeyRelatedField(queryset=MenuItem.objects.filter(is_active=True))
    quantity = serializers.IntegerField(min_value=1)


class ComboSerializer(ValidityPeriodSerializerMixin, serializers.ModelSerializer):
    """
    `items` is written as a JSON list of {product_id, quantity} — sent as text
    because the request is multipart (it carries the image) — and returned
    nested on read together with `regular_price`, the price of the products
    bought separately.
    """
    items = serializers.JSONField(binary=True, write_only=True)
    price = serializers.DecimalField(
        max_digits=6, decimal_places=2, min_value=Decimal('0.01'),
        error_messages={'min_value': _('El precio del combo debe ser mayor a 0.')},
    )
    institution_name = serializers.CharField(source='institution.name', read_only=True)
    service = ComboService

    class Meta:
        model = Combo
        fields = [
            'id', 'name', 'price', 'image', 'start_date', 'end_date',
            'items', 'status', 'institution_name',
        ]

    def validate_items(self, value):
        if not isinstance(value, list) or not value:
            raise serializers.ValidationError(_('Agrega al menos un producto al combo.'))
        items = ComboItemInputSerializer(data=value, many=True)
        items.is_valid(raise_exception=True)
        pairs = [(row['product_id'], row['quantity']) for row in items.validated_data]
        products = [product for product, _quantity in pairs]
        if len({product.id for product in products}) != len(products):
            raise serializers.ValidationError(_('Un producto aparece más de una vez en el combo.'))
        institution = self.context.get('institution')
        if institution and any(product.institution_id != institution.id for product in products):
            raise serializers.ValidationError(_('Uno o más productos no pertenecen a esta institución.'))
        return pairs

    def validate(self, attrs):
        self.validate_period(attrs)
        return attrs

    def create(self, validated_data):
        items = validated_data.pop('items')
        combo = super().create(validated_data)
        ComboService.set_items(combo, items)
        return combo

    def update(self, instance, validated_data):
        items = validated_data.pop('items', None)
        combo = super().update(instance, validated_data)
        if items is not None:
            ComboService.set_items(combo, items)
        return combo

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['items'] = ComboItemSerializer(instance.items.all(), many=True).data
        data['regular_price'] = str(instance.regular_price())
        return data
