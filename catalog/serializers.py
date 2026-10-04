from rest_framework import serializers
from .models import MenuItem, Allergen, Ingredient
from .mixins import IngredientValidationMixin


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
            raise serializers.ValidationError('Ya existe un alérgeno con ese nombre.')
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
            raise serializers.ValidationError('Ya existe un ingrediente con ese nombre.')
        return value

    def create(self, validated_data):
        if 'allergens' in self.initial_data:
            validated_data['allergens_reviewed'] = True
        return super().create(validated_data)


class MenuItemSerializer(IngredientValidationMixin, serializers.ModelSerializer):
    """`ingredients` is writable as a list of Ingredient ids. `allergens` is
    NEVER set directly by the client — it's auto-computed as the union of
    the selected ingredients' allergens, so allergen safety never depends
    on an admin remembering to tag it manually. On read, both fields
    return nested objects (id + name), not bare ids.

    is_active/is_visible are declared explicitly (instead of relying on
    ModelSerializer's automatic introspection) because DRF's default
    handling for BooleanField can be unreliable on multipart/form-data
    requests — without this, a new product created with an image upload
    could come back with both False despite the model's default=True."""

    is_active = serializers.BooleanField(default=True, required=False)
    is_visible = serializers.BooleanField(default=True, required=False)

    class Meta:
        model = MenuItem
        fields = [
            'id', 'name', 'description', 'category', 'image', 'price',
            'is_active', 'is_visible', 'stock', 'ingredients', 'allergens',
        ]
        read_only_fields = ['allergens']

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['ingredients'] = IngredientSerializer(instance.ingredients.all(), many=True).data
        data['allergens'] = AllergenSerializer(instance.allergens.all(), many=True).data
        return data

    def _sync_allergens(self, instance):
        allergen_ids = set()
        for ingredient in instance.ingredients.all():
            allergen_ids.update(ingredient.allergens.values_list('id', flat=True))
        instance.allergens.set(allergen_ids)

    def create(self, validated_data):
        instance = super().create(validated_data)
        self._sync_allergens(instance)
        return instance

    def update(self, instance, validated_data):
        instance = super().update(instance, validated_data)
        self._sync_allergens(instance)
        return instance