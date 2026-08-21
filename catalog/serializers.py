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

class MenuItemSerializer(IngredientValidationMixin, serializers.ModelSerializer):
    class Meta:
        model = MenuItem
        fields = ['id', 'name', 'description', 'price', 'is_active', 'is_visible', 'stock', 'ingredients', 'allergens']
