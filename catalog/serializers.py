from rest_framework import serializers
from .models import MenuItem, Allergen

class AllergenSerializer(serializers.ModelSerializer):
    class Meta:
        model = Allergen
        fields = ['id', 'name', 'description']

class MenuItemSerializer(serializers.ModelSerializer):
    allergens = AllergenSerializer(many=True, read_only=True)

    class Meta:
        model = MenuItem
        fields = ['id', 'name', 'description', 'price', 'is_active', 'allergens']
