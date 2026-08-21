from django.db import models
from django.utils.translation import gettext_lazy as _
from users.models import StudentProfile
from catalog.models import MenuItem

class PreOrder(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', _('Pending')
        DELIVERED = 'DELIVERED', _('Delivered')
        CANCELED = 'CANCELED', _('Canceled')

    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name='pre_orders')
    total_amount = models.DecimalField(_('total amount'), max_digits=8, decimal_places=2)
    status = models.CharField(_('status'), max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('pre-order')
        verbose_name_plural = _('pre-orders')

    def __str__(self):
        return f'PreOrder {self.id} for {self.student.user.username}'

class PreOrderItem(models.Model):
    pre_order = models.ForeignKey(PreOrder, on_delete=models.CASCADE, related_name='items')
    menu_item = models.ForeignKey(MenuItem, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=1)
    price_at_purchase = models.DecimalField(_('price at purchase'), max_digits=6, decimal_places=2)

    def __str__(self):
        return f'{self.quantity}x {self.menu_item.name}'
