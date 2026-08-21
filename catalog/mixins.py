from rest_framework.exceptions import ValidationError

class IngredientValidationMixin:
    # Intercepta la validación para asegurar que haya ingredientes
    def validate(self, attrs):
        request = self.context.get('request')
        if request and request.method in ['POST', 'PUT', 'PATCH']:
            ingredients = request.data.get('ingredients')
            if not ingredients or len(ingredients) == 0:
                raise ValidationError({'ingredients': 'You must provide at least one ingredient for the product.'})
        return super().validate(attrs)
