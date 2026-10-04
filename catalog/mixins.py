from rest_framework.exceptions import ValidationError
from .models import Ingredient


class IngredientValidationMixin:
    # Intercepta la validación para asegurar que haya ingredientes
    # y que todos tengan sus alérgenos confirmados por un admin.
    def validate(self, attrs):
        request = self.context.get('request')
        if request and request.method in ['POST', 'PUT', 'PATCH']:
            ingredients = request.data.get('ingredients')
            if not ingredients or len(ingredients) == 0:
                raise ValidationError({'ingredients': 'You must provide at least one ingredient for the product.'})

            unreviewed = list(
                Ingredient.objects.filter(
                    id__in=ingredients, allergens_reviewed=False
                ).values_list('name', flat=True)
            )
            if unreviewed:
                raise ValidationError({
                    'ingredients': (
                        'Estos ingredientes no tienen sus alérgenos confirmados: '
                        f'{", ".join(unreviewed)}. Confírmalos en "Gestionar '
                        'alérgenos por ingrediente" antes de guardar el producto.'
                    )
                })
        return super().validate(attrs)