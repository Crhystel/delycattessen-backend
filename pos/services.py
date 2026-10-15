from decimal import Decimal

from django.db import transaction
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import PermissionDenied, ValidationError

from catalog.models import MenuItem
from users.models import CustomUser
from users.permissions import get_object_institution_id
from wallet.models import Transaction, Wallet

from .mixins import AllergenValidatorMixin, ParentalControlValidatorMixin
from .models import Sale, SaleItem


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
        total = sale.total_amount
        wallet = Wallet.objects.select_for_update().get(student=self.customer.student_profile)
        if wallet.balance < total:
            raise ValidationError(
                _('Saldo insuficiente. Tiene $%(balance)s y el total es $%(total)s') % {
                    'balance': wallet.balance, 'total': total,
                }
            )
        wallet.balance -= total
        wallet.save(update_fields=['balance', 'updated_at'])
        Transaction.objects.create(
            wallet=wallet,
            amount=total,
            status=Transaction.Status.SUCCESS,
            type=Transaction.Type.CONSUMPTION,
            sale=sale,
        )


PROCESSORS = {
    Sale.PaymentMethod.CASH: CashSaleProcessor,
    Sale.PaymentMethod.CREDIT: CreditSaleProcessor,
    Sale.PaymentMethod.WALLET: WalletSaleProcessor,
}


def process_sale(payment_method, institution, cashier, customer, lines):
    return PROCESSORS[payment_method](institution, cashier, customer).process(lines)
