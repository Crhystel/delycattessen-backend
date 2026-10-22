from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _
from users.models import StudentProfile


class PreOrder(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', _('Pending')
        DELIVERED = 'DELIVERED', _('Delivered')
        CANCELED = 'CANCELED', _('Canceled')

    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name='pre_orders')
    daily_menu = models.ForeignKey(
        'catalog.DailyMenu',
        on_delete=models.PROTECT,
        related_name='pre_orders',
    )
    total_amount = models.DecimalField(_('total amount'), max_digits=8, decimal_places=2)
    status = models.CharField(_('status'), max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('pre-order')
        verbose_name_plural = _('pre-orders')
        indexes = [models.Index(fields=['created_at'])]

    def __str__(self):
        return f'PreOrder {self.id} for {self.student.user.username}'

    @property
    def delivery_date(self):
        return self.daily_menu.date


class PreOrderItem(models.Model):
    pre_order = models.ForeignKey(PreOrder, on_delete=models.CASCADE, related_name='items')
    dish = models.ForeignKey(
        'catalog.MenuDish',
        on_delete=models.PROTECT,
        related_name='pre_order_items',
    )
    quantity = models.PositiveIntegerField(default=1)
    price_at_purchase = models.DecimalField(_('price at purchase'), max_digits=6, decimal_places=2)

    def __str__(self):
        return f'{self.quantity}x {self.dish.name}'


class Sale(models.Model):
    class PaymentMethod(models.TextChoices):
        CASH = 'CASH', _('Efectivo')
        CREDIT = 'CREDIT', _('Crédito institucional')
        WALLET = 'WALLET', _('Saldo de billetera')

    institution = models.ForeignKey(
        'users.Institution', verbose_name=_('institution'),
        on_delete=models.PROTECT, related_name='sales',
    )
    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_('cashier'),
        on_delete=models.PROTECT, related_name='processed_sales',
    )
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_('customer'),
        on_delete=models.PROTECT, related_name='purchases', null=True, blank=True,
        help_text=_('Empty for quick sales (cash, nobody identified).'),
    )
    payment_method = models.CharField(_('payment method'), max_length=10, choices=PaymentMethod.choices)
    total_amount = models.DecimalField(_('total amount'), max_digits=8, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('sale')
        verbose_name_plural = _('sales')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['customer', 'created_at']),
            models.Index(fields=['institution', 'created_at']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(payment_method='CASH') | models.Q(customer__isnull=False),
                name='sale_credit_and_wallet_require_customer',
            ),
        ]

    def __str__(self):
        return f'Sale {self.id} ({self.payment_method}) {self.total_amount}'


class SaleItem(models.Model):
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name='items')
    menu_item = models.ForeignKey('catalog.MenuItem', on_delete=models.PROTECT, related_name='sale_items')
    quantity = models.PositiveIntegerField()
    price_at_purchase = models.DecimalField(_('price at purchase'), max_digits=6, decimal_places=2)

    def __str__(self):
        return f'{self.quantity}x {self.menu_item.name}'
