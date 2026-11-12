from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from users.models import CustomUser, StudentProfile

from .services import sync_allergens_from_ingredients


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
        sync_allergens_from_ingredients(instance)

    def create(self, validated_data):
        instance = super().create(validated_data)
        self._sync_allergens(instance)
        return instance

    def update(self, instance, validated_data):
        instance = super().update(instance, validated_data)
        self._sync_allergens(instance)
        return instance


class ViewerAllergenContextMixin:
    """
    Mixin Pattern (over DRF's get_serializer_context): hands serializers the ids
    of the allergens that affect whoever is asking, as `viewer_allergy_ids`, so
    the server marks the conflicting products instead of every client doing it.

    - STUDENT: their own allergies. TEACHER: their own allergies.
    - PARENT: the allergies of the child in ?student_id=, only if it is their
      own child (ViewerInstitutionMixin already rejects anyone else's).
    - Cashier / admin: none, they do not buy for themselves here.
    """
    def get_viewer_allergy_ids(self) -> set[int]:
        user = self.request.user
        if user.role == CustomUser.Role.STUDENT:
            owner = getattr(user, 'student_profile', None)
        elif user.role == CustomUser.Role.TEACHER:
            owner = user
        elif user.role == CustomUser.Role.PARENT:
            owner = self._viewer_child()
        else:
            owner = None
        if owner is None:
            return set()
        return set(owner.allergies.values_list('id', flat=True))

    def _viewer_child(self):
        try:
            student_id = int(self.request.query_params.get('student_id'))
        except (TypeError, ValueError):
            return None
        return StudentProfile.objects.active().filter(pk=student_id, parent__user=self.request.user).first()

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['viewer_allergy_ids'] = self.get_viewer_allergy_ids()
        return context


class ViewerAllergenFieldsMixin(serializers.Serializer):
    """
    Mixin Pattern: `has_viewer_allergens` and `conflicting_allergens` (names),
    computed against `context['viewer_allergy_ids']`. Without that context (admin
    screens) nothing is marked. Subclasses whose allergens live elsewhere
    override `allergen_source`.
    """
    has_viewer_allergens = serializers.SerializerMethodField()
    conflicting_allergens = serializers.SerializerMethodField()

    def allergen_source(self, obj):
        return obj

    def viewer_allergy_ids(self) -> set[int]:
        return self.context.get('viewer_allergy_ids') or set()

    def get_conflicting_allergens(self, obj) -> list[str]:
        viewer_ids = self.viewer_allergy_ids()
        if not viewer_ids:
            return []
        return [a.name for a in self.allergen_source(obj).allergens.all() if a.id in viewer_ids]

    def get_has_viewer_allergens(self, obj) -> bool:
        return bool(self.get_conflicting_allergens(obj))


class ValidityPeriodSerializerMixin(serializers.Serializer):
    """
    Mixin Pattern: `status` (derived from today, never stored) and the date
    rules shared by every offer with a validity period (combos).
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
