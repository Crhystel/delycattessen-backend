from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Sum
from django.db.models.functions import TruncDate, TruncMonth, TruncWeek
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import PermissionDenied, ValidationError

from catalog.models import DailyMenu, MenuItem, MonthlyMenu
from catalog.services import ComboService, PromotionService
from core.timezone import LOCAL_TIMEZONE, local_now, local_today
from users.models import CustomUser, StudentProfile
from users.permissions import get_object_institution_id
from wallet.services import debit_wallet

from .mixins import AllergenValidatorMixin, ParentalControlValidatorMixin
from .models import PreOrder, PreOrderItem, Sale, SaleCombo, SaleItem


class SaleProcessor(AllergenValidatorMixin):
    """
    Template Method: process() fixes the sale flow (lock products -> validate
    customer -> register sale -> charge -> discount stock). Subclasses only
    change who can be the customer and how the sale is charged. Combos are
    one line at their fixed price, but their products are locked, checked for
    allergens and discounted from stock like the rest.
    """
    payment_method = None
    allowed_roles = ()
    customer_required = True
    role_error = _('Este cliente no puede usar este método de pago.')

    def __init__(self, institution, cashier, customer=None):
        self.institution = institution
        self.cashier = cashier
        self.customer = customer

    @transaction.atomic
    def process(self, lines, combo_lines=()):
        """`lines` is a list of (menu_item_id, quantity); `combo_lines` of (combo_id, quantity)."""
        combos = ComboService.combos_for_sale(self.institution, combo_lines)
        locked = self._lock_items(list(lines) + [
            (combo_item.menu_item_id, combo_item.quantity * quantity)
            for combo, quantity in combos
            for combo_item in combo.items.all()
        ])
        items = self._standalone_items(lines, locked)
        # Promotions in force today lower the charged price (HU-03.3).
        prices = PromotionService.unit_prices([item for item, _quantity in items])
        total = sum((prices[item.id] * quantity for item, quantity in items), Decimal('0.00'))
        total += sum((combo.price * quantity for combo, quantity in combos), Decimal('0.00'))

        self.validate_customer(locked, total)

        sale = Sale.objects.create(
            institution=self.institution,
            cashier=self.cashier,
            customer=self.customer,
            payment_method=self.payment_method,
            total_amount=total,
        )
        SaleItem.objects.bulk_create([
            SaleItem(sale=sale, menu_item=item, quantity=quantity, price_at_purchase=prices[item.id])
            for item, quantity in items
        ])
        SaleCombo.objects.bulk_create([
            SaleCombo(sale=sale, combo=combo, name=combo.name, quantity=quantity, price_at_purchase=combo.price)
            for combo, quantity in combos
        ])
        self.charge(sale)
        self._discount_stock(locked)
        return sale

    def validate_customer(self, items, total):
        if self.customer is None:
            if self.customer_required:
                raise ValidationError({'user_id': _('Este método de pago requiere identificar al cliente.')})
            return
        if self.customer.role not in self.allowed_roles:
            raise PermissionDenied(self.role_error)
        if not self.customer.is_active or get_object_institution_id(self.customer) != self.institution.id:
            raise ValidationError({'user_id': _('El cliente no pertenece a esta institución.')})
        self.validate_allergens(self.allergy_owner(), [item for item, _quantity in items])

    def allergy_owner(self):
        """Object whose `allergies` are checked against the products."""
        return self.customer

    def charge(self, sale):
        """Money movement; nothing to do unless the payment method moves money."""

    def _lock_items(self, lines):
        quantities = {}
        for menu_item_id, quantity in lines:
            quantities[menu_item_id] = quantities.get(menu_item_id, 0) + quantity

        found = {
            item.id: item
            for item in MenuItem.objects.select_for_update().filter(
                id__in=sorted(quantities), institution=self.institution, is_active=True,
            ).order_by('id')
        }
        if len(found) != len(quantities):
            raise ValidationError({'items': _('Uno o más productos no existen en esta institución.')})

        items = []
        for menu_item_id, quantity in quantities.items():
            item = found[menu_item_id]
            if item.stock < quantity:
                raise ValidationError({
                    'items': _('Stock insuficiente de "%(name)s".') % {'name': item.name}
                })
            items.append((item, quantity))
        return items

    @staticmethod
    def _standalone_items(lines, locked):
        """Products bought on their own (outside combos), as (item, quantity)."""
        quantities = {}
        for menu_item_id, quantity in lines:
            quantities[menu_item_id] = quantities.get(menu_item_id, 0) + quantity
        by_id = {item.id: item for item, _quantity in locked}
        return [(by_id[menu_item_id], quantity) for menu_item_id, quantity in quantities.items()]

    def _discount_stock(self, items):
        for item, quantity in items:
            new_stock = item.stock - quantity
            MenuItem.objects.filter(pk=item.pk).update(stock=new_stock, is_visible=new_stock > 0)


class CashSaleProcessor(SaleProcessor):
    """Cash payment. Without a customer it is a quick sale."""
    payment_method = Sale.PaymentMethod.CASH
    allowed_roles = (CustomUser.Role.STUDENT, CustomUser.Role.TEACHER)
    customer_required = False

    def allergy_owner(self):
        return getattr(self.customer, 'student_profile', None) or self.customer


class CreditSaleProcessor(SaleProcessor):
    """Institutional credit: teachers only. The consumption stays on their name."""
    payment_method = Sale.PaymentMethod.CREDIT
    allowed_roles = (CustomUser.Role.TEACHER,)
    role_error = _('Solo los docentes pueden comprar a crédito.')


class WalletSaleProcessor(ParentalControlValidatorMixin, SaleProcessor):
    """Wallet balance: students only, subject to parental controls."""
    payment_method = Sale.PaymentMethod.WALLET
    allowed_roles = (CustomUser.Role.STUDENT,)
    role_error = _('Solo los estudiantes pueden pagar con saldo.')

    def allergy_owner(self):
        return self.customer.student_profile

    def validate_customer(self, items, total):
        super().validate_customer(items, total)
        self.validate_parental_controls(self.customer.student_profile, total)

    def charge(self, sale):
        debit_wallet(self.customer.student_profile.wallet, sale.total_amount, sale=sale)


PROCESSORS = {
    Sale.PaymentMethod.CASH: CashSaleProcessor,
    Sale.PaymentMethod.CREDIT: CreditSaleProcessor,
    Sale.PaymentMethod.WALLET: WalletSaleProcessor,
}


def process_sale(payment_method, institution, cashier, customer, lines, combo_lines=()):
    return PROCESSORS[payment_method](institution, cashier, customer).process(lines, combo_lines)


def month_start_local(now=None):
    """First instant of the current month in the institution's timezone."""
    now = now or local_now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def monthly_credit_total(customer):
    """What a teacher has accumulated on institutional credit this month."""
    total = Sale.objects.filter(
        customer=customer,
        payment_method=Sale.PaymentMethod.CREDIT,
        created_at__gte=month_start_local(),
    ).aggregate(total=Sum('total_amount'))['total']
    return total or Decimal('0.00')


class PreOrderCreator(AllergenValidatorMixin, ParentalControlValidatorMixin):
    """
    Creates a pre-order for a child of the authenticated parent: checks the
    delivery date, the published menu, the dishes, parental controls and
    allergens, then debits the wallet. Everything runs in one transaction.
    """

    def __init__(self, parent_profile):
        self.parent_profile = parent_profile

    @transaction.atomic
    def create(self, student_id, delivery_date, items_data):
        student = self._get_own_student(student_id)

        if delivery_date <= local_today():
            raise ValidationError(
                _("No se puede preordenar para hoy o fechas pasadas. La fecha de entrega debe ser posterior a hoy.")
            )

        daily_menu = self._get_published_menu(student, delivery_date)
        order_lines, total_amount = self._build_lines(daily_menu, items_data)

        self.validate_parental_controls(student, total_amount, target_date=delivery_date)
        self.validate_allergens(student, [dish for dish, _quantity in order_lines])

        pre_order = PreOrder.objects.create(
            student=student,
            daily_menu=daily_menu,
            total_amount=total_amount,
            status=PreOrder.Status.PENDING,
        )
        PreOrderItem.objects.bulk_create([
            PreOrderItem(pre_order=pre_order, dish=dish, quantity=quantity, price_at_purchase=dish.price)
            for dish, quantity in order_lines
        ])
        debit_wallet(student.wallet, total_amount, pre_order=pre_order)
        return pre_order

    def _get_own_student(self, student_id):
        try:
            student = StudentProfile.objects.select_related('institution', 'wallet', 'parent').get(id=student_id)
        except StudentProfile.DoesNotExist:
            raise ValidationError({'student_id': _('Estudiante no encontrado.')})
        if not self.parent_profile or student.parent_id != self.parent_profile.id:
            raise PermissionDenied(_('No tienes permiso sobre este estudiante.'))
        return student

    @staticmethod
    def _get_published_menu(student, delivery_date):
        try:
            return DailyMenu.objects.select_related('monthly_menu').get(
                date=delivery_date,
                monthly_menu__institution=student.institution,
                monthly_menu__status=MonthlyMenu.Status.PUBLISHED,
            )
        except DailyMenu.DoesNotExist:
            raise ValidationError(
                _("No hay un menú publicado para esa fecha en la institución del estudiante.")
            )

    @staticmethod
    def _build_lines(daily_menu, items_data):
        available_dishes = {dish.id: dish for dish in daily_menu.dishes.filter(is_active=True)}
        order_lines = []
        total_amount = Decimal('0.00')
        for item_data in items_data:
            dish = available_dishes.get(item_data['dish_id'])
            if dish is None:
                raise ValidationError(
                    _("El platillo solicitado no está disponible en el menú de esa fecha.")
                )
            order_lines.append((dish, item_data['quantity']))
            total_amount += dish.price * item_data['quantity']
        return order_lines, total_amount


def deliver_preorder(pre_order):
    """Marks a pending pre-order as delivered; only on its own delivery day."""
    if pre_order.status != PreOrder.Status.PENDING:
        raise ValidationError({'detail': _("Esta preorden no está pendiente de entrega.")})
    if pre_order.daily_menu.date != local_today():
        raise ValidationError({'detail': _("Esta preorden es para el %(date)s, no para hoy.") % {
            'date': pre_order.daily_menu.date.strftime('%d/%m/%Y'),
        }})
    pre_order.status = PreOrder.Status.DELIVERED
    pre_order.save(update_fields=['status'])
    return pre_order


def consume_qr_nonce(nonce):
    """Marks a QR nonce as used. False when it was already used (replay)."""
    return cache.add(f'used_qr_nonce_{nonce}', True, timeout=settings.QR_NONCE_TTL_SECONDS)


def build_identified_user_payload(user, method):
    """Standard summary of an identified person (face or QR) for the POS screen."""
    balance = '0.00'
    allergies = [a.name for a in user.allergies.all()]
    student_id = None
    institution = user.institution  # staff/teacher institution
    pending_orders = []

    if hasattr(user, 'student_profile'):
        student_profile = user.student_profile
        student_id = student_profile.id
        allergies = [a.name for a in student_profile.allergies.all()]
        # A student's institution lives on StudentProfile, not on the
        # CustomUser itself (that field is only populated for staff).
        institution = student_profile.institution
        if hasattr(student_profile, 'wallet'):
            balance = str(student_profile.wallet.balance)

        pending_preorders = PreOrder.objects.filter(
            student=student_profile,
            status=PreOrder.Status.PENDING,
            daily_menu__date=local_today(),
        ).select_related('daily_menu').prefetch_related('items__dish').order_by('created_at')

        pending_orders = [
            {
                'pre_order_id': pre_order.id,
                'total_amount': str(pre_order.total_amount),
                'created_at': pre_order.created_at.isoformat(),
                'delivery_date': str(pre_order.delivery_date),
                'items': [
                    {
                        'dish_name': item.dish.name,
                        'quantity': item.quantity,
                        'price_at_purchase': str(item.price_at_purchase),
                    }
                    for item in pre_order.items.all()
                ],
            }
            for pre_order in pending_preorders
        ]
    elif hasattr(user, 'wallet'):
        balance = str(user.wallet.balance)

    # Teachers do not use a wallet: the cashier sees what they owe on credit.
    credit_accumulated = None
    if user.role == CustomUser.Role.TEACHER:
        credit_accumulated = str(monthly_credit_total(user))

    return {
        'user_id': user.id,
        'student_id': student_id,
        'username': user.username,
        'full_name': f'{user.first_name} {user.last_name}'.strip() or user.username,
        'role': user.role,
        'role_display': user.get_role_display(),
        'institution': institution.name if institution else None,
        'balance': balance,
        'credit_accumulated': credit_accumulated,
        'allergies': sorted(set(allergies)),
        'pending_orders': pending_orders,
        'identification_method': method,
    }


CHANNEL_PREORDER = 'PREORDER'
# Sales channels shown on the dashboard, in display order: the three POS payment
# methods plus pre-orders (always paid from the wallet when they are created).
CHANNELS = (
    Sale.PaymentMethod.CASH.value,
    Sale.PaymentMethod.WALLET.value,
    Sale.PaymentMethod.CREDIT.value,
    CHANNEL_PREORDER,
)
GRANULARITIES = ('day', 'week', 'month')
_ZERO = Decimal('0.00')
_TRUNC_BY_GRANULARITY = {'day': TruncDate, 'week': TruncWeek, 'month': TruncMonth}
_LINE_TOTAL = ExpressionWrapper(
    F('quantity') * F('price_at_purchase'),
    output_field=DecimalField(max_digits=12, decimal_places=2),
)


def _money(value):
    return str(Decimal(value).quantize(_ZERO))


def _change_pct(current, previous):
    """Percentage change against the previous period; None when there is nothing to compare."""
    if not previous:
        return None
    return round(float((Decimal(current) - Decimal(previous)) / Decimal(previous) * 100), 1)


def _local_day_start(day):
    return datetime.combine(day, time.min, tzinfo=LOCAL_TIMEZONE)


def _bucket_start(day, granularity):
    if granularity == 'week':
        return day - timedelta(days=day.weekday())
    if granularity == 'month':
        return day.replace(day=1)
    return day


def _next_bucket_start(start, granularity):
    if granularity == 'week':
        return start + timedelta(days=7)
    if granularity == 'month':
        return (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start + timedelta(days=1)


class SalesAnalytics:
    """
    Read-only sales metrics for the admin dashboard over an inclusive range of
    local dates. `institution=None` consolidates every institution.

    Revenue is every Sale (POS: cash, wallet, credit) plus every pre-order that
    is not canceled, counted when it is created. Wallet consumption transactions
    are not added (they duplicate the sale) and recharges are not revenue.
    Days are cut in the business time zone, not in UTC.
    """

    def __init__(self, institution, date_from, date_to):
        self.institution = institution
        self.date_from = date_from
        self.date_to = date_to
        days = (date_to - date_from).days + 1
        self.previous_to = date_from - timedelta(days=1)
        self.previous_from = self.previous_to - timedelta(days=days - 1)

    # ---- querysets ------------------------------------------------------
    @staticmethod
    def _bounds(date_from, date_to):
        return _local_day_start(date_from), _local_day_start(date_to + timedelta(days=1))

    def _sales(self, date_from, date_to):
        start, end = self._bounds(date_from, date_to)
        sales = Sale.objects.filter(created_at__gte=start, created_at__lt=end)
        if self.institution is not None:
            sales = sales.filter(institution=self.institution)
        return sales

    def _preorders(self, date_from, date_to):
        start, end = self._bounds(date_from, date_to)
        preorders = PreOrder.objects.filter(created_at__gte=start, created_at__lt=end).exclude(
            status=PreOrder.Status.CANCELED,
        )
        if self.institution is not None:
            preorders = preorders.filter(student__institution=self.institution)
        return preorders

    # ---- summary --------------------------------------------------------
    def _channel_totals(self, date_from, date_to):
        totals = {channel: {'amount': _ZERO, 'count': 0} for channel in CHANNELS}
        rows = self._sales(date_from, date_to).order_by().values('payment_method').annotate(
            amount=Sum('total_amount'), count=Count('id'),
        )
        for row in rows:
            totals[row['payment_method']] = {'amount': row['amount'], 'count': row['count']}
        preorders = self._preorders(date_from, date_to).aggregate(amount=Sum('total_amount'), count=Count('id'))
        totals[CHANNEL_PREORDER] = {'amount': preorders['amount'] or _ZERO, 'count': preorders['count']}
        return totals

    def _window(self, date_from, date_to):
        channels = self._channel_totals(date_from, date_to)
        revenue = sum((data['amount'] for data in channels.values()), _ZERO)
        transactions = sum(data['count'] for data in channels.values())
        average_ticket = revenue / transactions if transactions else _ZERO
        return {
            'channels': channels,
            'revenue': revenue,
            'transactions': transactions,
            'average_ticket': average_ticket,
        }

    @staticmethod
    def _kpi(current, previous, fmt=lambda value: value):
        return {'current': fmt(current), 'previous': fmt(previous), 'change_pct': _change_pct(current, previous)}

    def summary(self):
        current = self._window(self.date_from, self.date_to)
        previous = self._window(self.previous_from, self.previous_to)
        revenue = current['revenue']
        channels = []
        for channel in CHANNELS:
            now, before = current['channels'][channel], previous['channels'][channel]
            channels.append({
                'channel': channel,
                'amount': _money(now['amount']),
                'count': now['count'],
                'share_pct': round(float(now['amount'] / revenue * 100), 1) if revenue else 0.0,
                'previous_amount': _money(before['amount']),
                'change_pct': _change_pct(now['amount'], before['amount']),
            })
        return {
            'period': {'date_from': self.date_from, 'date_to': self.date_to},
            'previous_period': {'date_from': self.previous_from, 'date_to': self.previous_to},
            'kpis': {
                'revenue': self._kpi(revenue, previous['revenue'], _money),
                'transactions': self._kpi(current['transactions'], previous['transactions']),
                'average_ticket': self._kpi(current['average_ticket'], previous['average_ticket'], _money),
            },
            'channels': channels,
        }

    # ---- time series ----------------------------------------------------
    def _bucket_rows(self, queryset, granularity, group_by):
        bucket = _TRUNC_BY_GRANULARITY[granularity]('created_at', tzinfo=LOCAL_TIMEZONE)
        return queryset.order_by().annotate(bucket=bucket).values('bucket', *group_by).annotate(
            amount=Sum('total_amount'), count=Count('id'),
        )

    @staticmethod
    def _as_local_date(value):
        return value.astimezone(LOCAL_TIMEZONE).date() if isinstance(value, datetime) else value

    def timeseries(self, granularity):
        """One entry per day/week/month of the range (empty ones included), with
        the amount per channel and the exact date range of the bucket so the
        dashboard can drill down by asking for that range."""
        buckets = {}
        start = _bucket_start(self.date_from, granularity)
        while start <= self.date_to:
            end = _next_bucket_start(start, granularity) - timedelta(days=1)
            buckets[start] = {
                'bucket_start': start,
                'date_from': max(start, self.date_from),
                'date_to': min(end, self.date_to),
                'revenue': _ZERO,
                'transactions': 0,
                'channels': {channel: _ZERO for channel in CHANNELS},
            }
            start = _next_bucket_start(start, granularity)

        sales = self._bucket_rows(self._sales(self.date_from, self.date_to), granularity, ('payment_method',))
        preorders = self._bucket_rows(self._preorders(self.date_from, self.date_to), granularity, ())
        for rows, channel_of in ((sales, lambda row: row['payment_method']), (preorders, lambda row: CHANNEL_PREORDER)):
            for row in rows:
                entry = buckets[self._as_local_date(row['bucket'])]
                entry['channels'][channel_of(row)] += row['amount']
                entry['revenue'] += row['amount']
                entry['transactions'] += row['count']

        return [
            {
                **entry,
                'revenue': _money(entry['revenue']),
                'channels': {channel: _money(amount) for channel, amount in entry['channels'].items()},
            }
            for entry in buckets.values()
        ]

    # ---- top products ---------------------------------------------------
    def _product_totals(self):
        """Units and revenue per product name, POS items and pre-order dishes
        together (they are different tables but the same dish to the admin)."""
        start, end = self._bounds(self.date_from, self.date_to)
        sale_items = SaleItem.objects.filter(sale__created_at__gte=start, sale__created_at__lt=end)
        preorder_items = PreOrderItem.objects.filter(
            pre_order__created_at__gte=start, pre_order__created_at__lt=end,
        ).exclude(pre_order__status=PreOrder.Status.CANCELED)
        if self.institution is not None:
            sale_items = sale_items.filter(sale__institution=self.institution)
            preorder_items = preorder_items.filter(pre_order__student__institution=self.institution)

        rows = list(
            sale_items.order_by().values(name=F('menu_item__name')).annotate(
                units=Sum('quantity'), revenue=Sum(_LINE_TOTAL),
            )
        ) + list(
            preorder_items.order_by().values(name=F('dish__name')).annotate(
                units=Sum('quantity'), revenue=Sum(_LINE_TOTAL),
            )
        )
        products = {}
        for row in rows:
            product = products.setdefault(
                row['name'].strip().casefold(), {'name': row['name'].strip(), 'units': 0, 'revenue': _ZERO},
            )
            product['units'] += row['units']
            product['revenue'] += row['revenue']
        return list(products.values())

    def top_products(self):
        return self._rankings(self._product_totals())

    # ---- combos -----------------------------------------------------------
    def top_combos(self):
        """Combos sold at the POS, each one as a single line at its own price
        (grouped by the name saved on the sale, so deleted combos still count),
        ranked like the products, plus the totals of the period."""
        start, end = self._bounds(self.date_from, self.date_to)
        lines = SaleCombo.objects.filter(sale__created_at__gte=start, sale__created_at__lt=end)
        if self.institution is not None:
            lines = lines.filter(sale__institution=self.institution)
        combos = list(
            lines.order_by().values('name').annotate(units=Sum('quantity'), revenue=Sum(_LINE_TOTAL))
        )
        return {
            **self._rankings(combos),
            'totals': {
                'units': sum(combo['units'] for combo in combos),
                'revenue': _money(sum((combo['revenue'] for combo in combos), _ZERO)),
            },
        }

    @staticmethod
    def _rankings(entries):
        """Top `ANALYTICS_TOP_PRODUCTS_LIMIT` of {name, units, revenue} rows by revenue and by units."""
        limit = settings.ANALYTICS_TOP_PRODUCTS_LIMIT

        def ranking(sort_key):
            ordered = sorted(entries, key=sort_key)[:limit]
            return [
                {'name': entry['name'], 'units': entry['units'], 'revenue': _money(entry['revenue'])}
                for entry in ordered
            ]

        return {
            'by_revenue': ranking(lambda entry: (-entry['revenue'], entry['name'])),
            'by_units': ranking(lambda entry: (-entry['units'], entry['name'])),
        }
