import calendar
import logging
from datetime import date
from functools import partial
from typing import Iterable, Sequence
from kombu.exceptions import OperationalError
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

from core.timezone import local_today

from .models import Allergen, Combo, ComboItem, MonthlyMenu, DailyMenu, MenuDish, MenuItem
from .tasks import send_combo_created_email, send_low_stock_email, send_monthly_menu_published_email

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


def _notify_combo_created(combo_id: int) -> None:
    """Enqueues the new-combo email. The combo is already committed, so an
    unreachable broker must not turn the creation into a 500."""
    try:
        send_combo_created_email.delay(combo_id)
    except (OperationalError, OSError):
        logger.exception('Could not enqueue new-combo email for combo %s', combo_id)


def _notify_low_stock(institution_id: int, product_ids: list[int], combo_ids: list[int]) -> None:
    """Enqueues the low stock email. The sale is already committed, so an
    unreachable broker must not turn it into a 500."""
    try:
        send_low_stock_email.delay(institution_id, product_ids, combo_ids)
    except (OperationalError, OSError):
        logger.exception('Could not enqueue low stock email for institution %s', institution_id)


def sync_allergens_from_ingredients(obj) -> None:
    """Stores on a MenuItem or MenuDish the union of its ingredients' allergens.
    POS sales and preorders check this denormalized list, so it must be
    recomputed whenever the product or one of its ingredients changes."""
    allergen_ids = Allergen.objects.filter(
        ingredients__in=obj.ingredients.all()
    ).values_list('id', flat=True)
    obj.allergens.set(set(allergen_ids))


def resync_allergens_for_ingredient(ingredient) -> None:
    """Recomputes the allergens of every product and dish that uses `ingredient`
    (active or not), so a change to the ingredient never leaves them stale."""
    with transaction.atomic():
        for obj in [*ingredient.menu_items.all(), *ingredient.menu_dishes.all()]:
            sync_allergens_from_ingredients(obj)


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


class ProductInUseByCombos(Exception):
    """The product still belongs to a combo that is in force or has not started.
    The view answers 409 with the combos in `combos` so the admin knows where
    to remove it first."""
    code = 'product_in_combos'

    def __init__(self, product, combos):
        self.combos = [{'id': combo.id, 'name': combo.name} for combo in combos]
        names = ', '.join(f'"{combo.name}"' for combo in combos)
        where = _('está en el combo %(names)s') if len(combos) == 1 else _('está en los combos %(names)s')
        self.message = _('No puedes eliminar "%(product)s": %(where)s. Quita el producto de esos combos primero.') % {
            'product': product.name, 'where': str(where) % {'names': names},
        }
        super().__init__(self.message)


class ComboService:
    """
    Service Layer Pattern: combos in force and their contents, shared by the
    catalog banner and the POS sale. Validity uses Ecuador's local date.
    """

    @classmethod
    def combos_using(cls, product: MenuItem) -> list[Combo]:
        """Combos that include `product` and have not expired yet (in force or
        scheduled). Expired ones are history and do not block anything."""
        return list(
            Combo.objects.filter(items__menu_item=product, end_date__gte=cls.today()).distinct().order_by('name')
        )

    @classmethod
    def ensure_product_unused(cls, product: MenuItem) -> None:
        """Raises ProductInUseByCombos while a combo that has not expired still
        includes `product`: deleting it would leave that combo with a dead item."""
        combos = cls.combos_using(product)
        if combos:
            raise ProductInUseByCombos(product, combos)

    @staticmethod
    def today() -> date:
        return local_today()

    @classmethod
    def in_force(cls, queryset=None):
        queryset = Combo.objects.all() if queryset is None else queryset
        return queryset.in_force(cls.today()).prefetch_related('items__menu_item')

    @staticmethod
    def available_units(combo: Combo, stocks: dict[int, int] | None = None) -> int:
        """How many units of `combo` can be sold with the stock of its products:
        the minimum of floor(stock / quantity) over its items. A combo without
        products or with an inactive product has none. `stocks` (product id ->
        stock) replaces the stored stock of those products."""
        stocks = stocks or {}
        units = [
            stocks.get(item.menu_item_id, item.menu_item.stock) // item.quantity if item.menu_item.is_active else 0
            for item in combo.items.all()
        ]
        return min(units, default=0)

    @staticmethod
    def short_items(pairs: Iterable[tuple[MenuItem, int]]) -> list[tuple[MenuItem, int]]:
        """(product, quantity) pairs whose product cannot cover one unit of the
        combo: stock below the quantity the combo needs."""
        return [(product, quantity) for product, quantity in pairs if product.stock < quantity]

    @classmethod
    def validate_stock(cls, pairs: Sequence[tuple[MenuItem, int]], current: dict[int, int] | None = None) -> None:
        """Rejects a combo whose products lack stock for one unit. `current` maps
        product id -> quantity the combo already has (when editing): only new
        products or a larger quantity are checked, so an existing combo can still
        be renamed or repriced after one of its products ran out."""
        current = current or {}
        to_check = [(product, quantity) for product, quantity in pairs if quantity > current.get(product.id, 0)]
        short = cls.short_items(to_check)
        if short:
            detail = ', '.join(
                _('"%(name)s" (disponibles %(stock)s, el combo necesita %(needed)s)')
                % {'name': product.name, 'stock': product.stock, 'needed': quantity}
                for product, quantity in short
            )
            raise ValidationError(_('Sin stock suficiente para el combo: %(detail)s.') % {'detail': detail})

    @classmethod
    def combos_for_sale(cls, institution, combo_lines) -> list[tuple[Combo, int]]:
        """`combo_lines` is a list of (combo_id, quantity); only combos in force
        in `institution` can be sold."""
        quantities: dict[int, int] = {}
        for combo_id, quantity in combo_lines:
            quantities[combo_id] = quantities.get(combo_id, 0) + quantity
        if not quantities:
            return []
        found = {
            combo.id: combo
            for combo in cls.in_force(Combo.objects.filter(institution=institution, id__in=quantities))
        }
        if len(found) != len(quantities):
            raise ValidationError({'combos': _('Uno o más combos no están vigentes en esta institución.')})
        return [(found[combo_id], quantity) for combo_id, quantity in quantities.items()]

    @staticmethod
    def notify_created(combo: Combo) -> None:
        """Emails the parents of the combo's institution once the creation commits.
        Only called on creation: editing a combo does not notify again."""
        transaction.on_commit(partial(_notify_combo_created, combo.id))

    @staticmethod
    @transaction.atomic
    def set_items(combo: Combo, items: Sequence[tuple[MenuItem, int]]) -> None:
        """Replaces the products of `combo` with (product, quantity) pairs."""
        combo.items.all().delete()
        ComboItem.objects.bulk_create([
            ComboItem(combo=combo, menu_item=product, quantity=quantity) for product, quantity in items
        ])


class LowStockService:
    """Emails the admins of the institution when a sale leaves a product or a
    combo in force at LOW_STOCK_THRESHOLD units or less, and again when it runs
    out. Only crossings alert: it is quiet while the stock stays low and alerts
    again after a restock."""

    @staticmethod
    def crossed(before: int, after: int) -> bool:
        """True when the stock enters the low level or runs out."""
        threshold = settings.LOW_STOCK_THRESHOLD
        return before > threshold >= after or before > 0 == after

    @classmethod
    def check(cls, institution, stock_changes: dict[int, tuple[int, int]]) -> None:
        """`stock_changes` maps product id -> (stock before, stock after) of a
        sale; call it after the new stock is saved, inside the sale transaction."""
        product_ids = sorted(pk for pk, (before, after) in stock_changes.items() if cls.crossed(before, after))
        before_stocks = {pk: before for pk, (before, _after) in stock_changes.items()}
        combos = ComboService.in_force(
            Combo.objects.filter(institution=institution, items__menu_item_id__in=stock_changes).distinct()
        )
        combo_ids = sorted(
            combo.id for combo in combos
            if cls.crossed(
                ComboService.available_units(combo, before_stocks), ComboService.available_units(combo),
            )
        )
        if product_ids or combo_ids:
            transaction.on_commit(partial(_notify_low_stock, institution.id, product_ids, combo_ids))
