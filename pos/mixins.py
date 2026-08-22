from rest_framework.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

class AllergenValidatorMixin:
    """
    Mixin to validate if the items in a preorder contain allergens
    that the student is allergic to.
    """
    def validate_allergens(self, student_profile, menu_items):
        student_allergies = student_profile.allergies.all()
        if not student_allergies.exists():
            return  # No allergies registered

        for item in menu_items:
            # Check intersection of item allergens and student allergies
            item_allergens = item.allergens.all()
            intersection = student_allergies.intersection(item_allergens)
            if intersection.exists():
                allergen_names = ', '.join([a.name for a in intersection])
                raise ValidationError(
                    _('El producto "%(product)s" contiene alérgenos que afectan al estudiante: %(allergens)s') % {
                        'product': item.name,
                        'allergens': allergen_names
                    }
                )
