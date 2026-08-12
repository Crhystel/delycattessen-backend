from django.db import models
from django.core.validators import MinValueValidator
from django.utils.translation import gettext_lazy as _

from users.models import StudentProfile


class Wallet(models.Model):
    student = models.OneToOneField(
        StudentProfile,
        on_delete=models.CASCADE,
        related_name='wallet',
    )
    balance = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
    )
    low_balance_threshold = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        help_text=_('Minimum balance that triggers a low-balance email alert.'),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Wallet({self.student_id}) - {self.balance}'


class Transaction(models.Model):
    class Gateway(models.TextChoices):
        KUSHKI = 'kushki', _('Kushki')
        PAYPHONE = 'payphone', _('Payphone')

    class Status(models.TextChoices):
        PENDING = 'pending', _('Pending')
        SUCCESS = 'success', _('Success')
        FAILED = 'failed', _('Failed')

    class Type(models.TextChoices):
        RECHARGE = 'recharge', _('Recharge')
        CONSUMPTION = 'consumption', _('Consumption')

    wallet = models.ForeignKey(
        Wallet,
        on_delete=models.CASCADE,
        related_name='transactions',
    )
    amount = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        validators=[MinValueValidator(0.01)],
    )
    gateway = models.CharField(
        max_length=10,
        choices=Gateway.choices,
        blank=True,  # left blank for 'consumption'-type transactions
    )
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
    )
    type = models.CharField(
        max_length=15,
        choices=Type.choices,
    )
    external_transaction_id = models.CharField(
        max_length=100,
        blank=True,
        help_text=_('Transaction ID returned by Kushki/Payphone.'),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.type} - {self.amount} ({self.status})'