from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError


class IngredientValidationMixin:
    # Intercepts validation to ensure the product has ingredients and that an
    # admin confirmed the allergens of every one of them.
    def validate(self, attrs):
        # A partial update that leaves the ingredients alone has nothing to check.
        if self.partial and 'ingredients' not in attrs:
            return super().validate(attrs)

        ingredients = attrs.get('ingredients') or []
        if not ingredients:
            raise ValidationError({'ingredients': _('Debe indicar al menos un ingrediente para el producto.')})

        unreviewed = [ingredient.name for ingredient in ingredients if not ingredient.allergens_reviewed]
        if unreviewed:
            raise ValidationError({
                'ingredients': _(
                    'Estos ingredientes no tienen sus alérgenos confirmados: %(names)s. '
                    'Confírmalos en "Gestionar alérgenos por ingrediente" antes de guardar el producto.'
                ) % {'names': ', '.join(unreviewed)}
            })
        return super().validate(attrs)


class AllergenSyncMixin:
    """
    Mixin Pattern: Encapsulates syncing allergens as the union of
    the allergens belonging to the item's ingredients.
    Used by MenuItemSerializer and MenuDishSerializer.
    """
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