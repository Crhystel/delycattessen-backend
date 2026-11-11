from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

from core.timezone import LOCAL_TIMEZONE, local_today
from users.models import StudentProfile
from wallet.models import Transaction


class AllergenValidatorMixin:
    """
    Mixin to validate if the items in a sale or preorder contain allergens
    that the owner of the allergies (a student profile or a teacher) is
    allergic to. There is no bypass: it applies to every payment method and
    every role that operates the sale.
    """
    def validate_allergens(self, owner, menu_items):
        owner_allergies = owner.allergies.all()
        if not owner_allergies.exists():
            return  # No allergies registered

        for item in menu_items:
            # Check intersection of item allergens and the owner's allergies
            item_allergens = item.allergens.all()
            intersection = owner_allergies.intersection(item_allergens)
            if intersection.exists():
                raise ValidationError(self.allergen_conflict_message(
                    owner, item.name, ', '.join([a.name for a in intersection])
                ))

    @staticmethod
    def allergen_conflict_message(owner, product, allergens):
        """The text is addressed to whoever reads it: the cashier (or parent)
        about a student, or the teacher about themselves."""
        params = {'product': product, 'allergens': allergens}
        if isinstance(owner, StudentProfile):
            return _(
                'El producto "%(product)s" contiene alérgenos que afectan al estudiante: %(allergens)s'
            ) % params
        return _('El producto "%(product)s" contiene alérgenos que te afectan: %(allergens)s') % params

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
                # Local day bounds: `created_at__date` would use UTC and miss the
                # purchases made after 19:00 local time.
                day_start = datetime.combine(today_ec, time.min, tzinfo=LOCAL_TIMEZONE)
                direct_sum = Transaction.objects.filter(
                    wallet=student.wallet,
                    type=Transaction.Type.CONSUMPTION,
                    status=Transaction.Status.SUCCESS,
                    created_at__gte=day_start,
                    created_at__lt=day_start + timedelta(days=1),
                    pre_order__isnull=True,
                ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
            else:
                direct_sum = Decimal('0.00')

            spent_target_date = preorders_sum + direct_sum
            if spent_target_date + Decimal(str(amount)) > control.daily_limit_amount:
                raise ValidationError(_("La preorden supera el límite de gasto diario configurado para ese día."))
