from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Sum
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import PermissionDenied, ValidationError

from catalog.models import DailyMenu, MenuItem, MonthlyMenu
from core.timezone import local_now, local_today
from users.models import CustomUser, StudentProfile
from users.permissions import get_object_institution_id
from wallet.services import debit_wallet

from .mixins import AllergenValidatorMixin, ParentalControlValidatorMixin
from .models import PreOrder, PreOrderItem, Sale, SaleItem


class SaleProcessor(AllergenValidatorMixin):
    """
    Template Method: process() fixes the sale flow (lock products -> validate
    customer -> register sale -> charge -> discount stock). Subclasses only
    change who can be the customer and how the sale is charged.
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
    def process(self, lines):
        """`lines` is a list of (menu_item_id, quantity)."""
        items = self._lock_items(lines)
        total = sum((item.price * quantity for item, quantity in items), Decimal('0.00'))

        self.validate_customer(items, total)

        sale = Sale.objects.create(
            institution=self.institution,
            cashier=self.cashier,
            customer=self.customer,
            payment_method=self.payment_method,
            total_amount=total,
        )
        SaleItem.objects.bulk_create([
            SaleItem(sale=sale, menu_item=item, quantity=quantity, price_at_purchase=item.price)
            for item, quantity in items
        ])
        self.charge(sale)
        self._discount_stock(items)
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


def process_sale(payment_method, institution, cashier, customer, lines):
    return PROCESSORS[payment_method](institution, cashier, customer).process(lines)


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
