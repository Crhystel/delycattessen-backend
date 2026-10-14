import calendar
import logging
from datetime import date
from functools import partial
from typing import Sequence
from kombu.exceptions import OperationalError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

from .models import MonthlyMenu, DailyMenu, MenuDish
from .tasks import send_monthly_menu_published_email

logger = logging.getLogger(__name__)


def _notify_menu_published(monthly_menu_id: int) -> None:
    """Enqueues the notification email. The menu is already committed as
    PUBLISHED, so an unreachable broker must not turn the publish into a 500."""
    try:
        send_monthly_menu_published_email.delay(monthly_menu_id)
    except (OperationalError, OSError):
        logger.exception(
            'Could not enqueue published-menu email for monthly menu %s', monthly_menu_id
        )


class MonthlyMenuService:
    """
    Service Layer Pattern: Encapsulates business logic for monthly menu
    lifecycle (generation of business days, daily dish allocation, publishing,
    and preorder validation rules).
    """

    @staticmethod
    def dish_has_pending_preorders(dish: MenuDish) -> bool:
        """Checks if the dish is referenced in any pending pre-orders."""
        from pos.models import PreOrder, PreOrderItem
        return PreOrderItem.objects.filter(
            dish=dish,
            pre_order__status=PreOrder.Status.PENDING,
        ).exists()

    @staticmethod
    def day_has_preorders(daily_menu: DailyMenu) -> bool:
        """Checks if any pre-orders exist for this daily menu."""
        from pos.models import PreOrder
        return PreOrder.objects.filter(daily_menu=daily_menu).exists()

    @classmethod
    def create_month(cls, institution, year: int, month: int, user) -> MonthlyMenu:
        """
        Creates a MonthlyMenu in DRAFT state and automatically generates
        DailyMenu records for all business days (Monday-Friday) of the month.
        """
        with transaction.atomic():
            monthly_menu = MonthlyMenu.objects.create(
                institution=institution,
                year=year,
                month=month,
                status=MonthlyMenu.Status.DRAFT,
                created_by=user,
            )
            _, num_days = calendar.monthrange(year, month)
            daily_menus = []
            for day in range(1, num_days + 1):
                current_date = date(year, month, day)
                # 0 = Monday ... 4 = Friday
                if current_date.weekday() < 5:
                    daily_menus.append(DailyMenu(monthly_menu=monthly_menu, date=current_date))

            DailyMenu.objects.bulk_create(daily_menus)
            return monthly_menu

    @classmethod
    def set_day_dishes(cls, daily_menu: DailyMenu, dishes: Sequence[MenuDish]) -> None:
        """
        Assigns dishes to a specific daily menu.
        Fails if preorders have already been placed for that day.
        """
        if cls.day_has_preorders(daily_menu):
            raise ValidationError(_("Este día ya tiene preórdenes y no se puede modificar."))
        daily_menu.dishes.set(dishes)

    @classmethod
    def publish(cls, monthly_menu: MonthlyMenu) -> MonthlyMenu:
        """
        Publishes a monthly menu and enqueues an asynchronous notification email
        to parents upon transaction commit. Requires at least one day with dishes.
        """
        has_dishes = monthly_menu.days.filter(dishes__isnull=False).exists()
        if not has_dishes:
            raise ValidationError(_("Asigna platillos al menos a un día antes de publicar."))

        monthly_menu.status = MonthlyMenu.Status.PUBLISHED
        monthly_menu.published_at = timezone.now()
        monthly_menu.save(update_fields=['status', 'published_at', 'updated_at'])

        transaction.on_commit(partial(_notify_menu_published, monthly_menu.id))
        return monthly_menu

    @classmethod
    def unpublish(cls, monthly_menu: MonthlyMenu) -> MonthlyMenu:
        """
        Reverts a published monthly menu back to DRAFT.
        Fails if any pre-orders have been created for any day of the menu.
        """
        from pos.models import PreOrder
        if PreOrder.objects.filter(daily_menu__monthly_menu=monthly_menu).exists():
            raise ValidationError(_("No se puede pasar a borrador: el menú ya tiene preórdenes."))

        monthly_menu.status = MonthlyMenu.Status.DRAFT
        monthly_menu.save(update_fields=['status', 'updated_at'])
        return monthly_menu
