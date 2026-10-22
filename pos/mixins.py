from decimal import Decimal

from django.db.models import Sum
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

from core.timezone import local_today
from wallet.models import Transaction


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

class ParentalControlValidatorMixin:
    def validate_parental_controls(self, student, amount, target_date=None):
        from users.models import ParentalControl
        from .models import PreOrder
        try:
            control = student.parental_control
        except (ParentalControl.DoesNotExist, AttributeError):
            return

        today_ec = local_today()
        if target_date is None:
            target_date = today_ec

        if control.allowed_days_enabled:
            # 0 = Monday, ..., 6 = Sunday
            if target_date.weekday() not in control.allowed_days:
                raise ValidationError(_("No está permitido comprar para ese día: está bloqueado por el control parental."))

        if control.daily_limit_enabled:
            preorders_sum = PreOrder.objects.filter(
                student=student,
                daily_menu__date=target_date,
            ).exclude(
                status=PreOrder.Status.CANCELED
            ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

            if target_date == today_ec:
                direct_sum = Transaction.objects.filter(
                    wallet=student.wallet,
                    type=Transaction.Type.CONSUMPTION,
                    status=Transaction.Status.SUCCESS,
                    created_at__date=today_ec,
                    pre_order__isnull=True,
                ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
            else:
                direct_sum = Decimal('0.00')

            spent_target_date = preorders_sum + direct_sum
            if spent_target_date + Decimal(str(amount)) > control.daily_limit_amount:
                raise ValidationError(_("La preorden supera el límite de gasto diario configurado para ese día."))
