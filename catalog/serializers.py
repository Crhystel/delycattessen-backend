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
    class Meta:
        model = Ingredient
        fields = ['id', 'name', 'description', 'allergens']


class IngredientCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ingredient
        fields = ['id', 'name', 'description', 'allergens']

    def validate_name(self, value):
        if Ingredient.objects.filter(name__iexact=value).exists():
            raise serializers.ValidationError('Ya existe un ingrediente con ese nombre.')
        return value


class MenuItemSerializer(IngredientValidationMixin, serializers.ModelSerializer):
    """`ingredients` is writable as a list of Ingredient ids. `allergens` is
    NEVER set directly by the client — it's auto-computed as the union of
    the selected ingredients' allergens, so allergen safety never depends
    on an admin remembering to tag it manually. On read, both fields
    return nested objects (id + name), not bare ids."""

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