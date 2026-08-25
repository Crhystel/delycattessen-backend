from rest_framework import serializers
from .models import MenuItem, Allergen, Ingredient
from .mixins import IngredientValidationMixin


class AllergenSerializer(serializers.ModelSerializer):
    class Meta:
        model = Allergen
        fields = ['id', 'name', 'description']


class IngredientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ingredient
        fields = ['id', 'name', 'description']


class IngredientCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ingredient
        fields = ['id', 'name', 'description']

    def validate_name(self, value):
        if Ingredient.objects.filter(name__iexact=value).exists():
            raise serializers.ValidationError('Ya existe un ingrediente con ese nombre.')
        return value


class MenuItemSerializer(IngredientValidationMixin, serializers.ModelSerializer):
    class Meta:
        model = MenuItem
        fields = [
            'id', 'name', 'description', 'category', 'image', 'price',
            'is_active', 'is_visible', 'stock', 'ingredients', 'allergens',
        ]