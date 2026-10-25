from django.db.models import Prefetch
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from .models import Promotion
from .services import PromotionService


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


class PromotionsInForceMixin:
    """
    Mixin Pattern (over DRF's get_queryset): prefetches today's promotions of
    each product into `promotions_in_force`, which MenuItemSerializer reads to
    show the discounted price without one query per product. Evaluated per
    request, so a promotion stops applying the day after it expires.
    """
    def get_queryset(self):
        return super().get_queryset().prefetch_related(
            Prefetch(
                'promotions',
                queryset=Promotion.objects.in_force(PromotionService.today()),
                to_attr='promotions_in_force',
            )
        )


class ValidityPeriodSerializerMixin(serializers.Serializer):
    """
    Mixin Pattern: `status` (derived from today, never stored) and the date
    rules shared by every offer with a validity period (discounts, combos).
    Subclasses set `service` to the class whose today() dates the offer.
    """
    service = None

    status = serializers.SerializerMethodField()

    def get_status(self, obj) -> str:
        return obj.status_on(self.service.today())

    def validate_period(self, attrs):
        """Returns the (start_date, end_date) the object will have after saving."""
        start_date = attrs.get('start_date', getattr(self.instance, 'start_date', None))
        end_date = attrs.get('end_date', getattr(self.instance, 'end_date', None))
        if start_date and end_date and end_date < start_date:
            raise ValidationError({'end_date': _('La fecha de fin no puede ser anterior a la de inicio.')})
        if self.instance is None and end_date and end_date < self.service.today():
            raise ValidationError({'end_date': _('La fecha de fin no puede estar en el pasado.')})
        return start_date, end_date
