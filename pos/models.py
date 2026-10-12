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
