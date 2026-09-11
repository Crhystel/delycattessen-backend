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

from django.utils import timezone
from decimal import Decimal
from wallet.models import Transaction

class ParentalControlValidatorMixin:
    def validate_parental_controls(self, student, amount):
        from users.models import ParentalControl
        try:
            control = student.parental_control
        except ParentalControl.DoesNotExist:
            return

        if control.allowed_days_enabled:
            current_day = timezone.localtime().weekday()
            if current_day not in control.allowed_days:
                raise ValidationError("Compras bloqueadas: Día no habilitado para este estudiante.")

        if control.daily_limit_enabled:
            today = timezone.localtime().date()
            transactions = Transaction.objects.filter(
                wallet=student.wallet,
                type=Transaction.Type.CONSUMPTION,
                status=Transaction.Status.SUCCESS,
                created_at__date=today
            )
            spent_today = sum([t.amount for t in transactions])
            if spent_today + Decimal(str(amount)) > control.daily_limit_amount:
                raise ValidationError("Compras bloqueadas: Límite diario excedido para este estudiante.")
