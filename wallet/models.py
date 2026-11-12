from django.db import models
from django.core.validators import MinValueValidator
from django.utils.translation import gettext_lazy as _

from users.models import StudentProfile


class Wallet(models.Model):
    # PROTECT: a wallet (balance and history) is never removed together with its owner.
    student = models.OneToOneField(
        StudentProfile,
        on_delete=models.PROTECT,
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

    class Meta:
        verbose_name = _('billetera')
        verbose_name_plural = _('billeteras')
        constraints = [
            # The validator only runs in forms and serializers; F() updates skip it.
            models.CheckConstraint(condition=models.Q(balance__gte=0), name='wallet_balance_not_negative'),
        ]

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
        on_delete=models.PROTECT,
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
    pre_order = models.ForeignKey(
        'pos.PreOrder',
        verbose_name=_('pre-order'),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='transactions',
    )
    sale = models.ForeignKey(
        'pos.Sale',
        verbose_name=_('sale'),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='transactions',
    )
    balance_after = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_('Wallet balance right after this movement; empty when pending, failed or not reconstructable.'),
    )
    claimed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_('When a Celery worker took this pending card recharge; empty while it waits in the queue.'),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = _('transacción')
        verbose_name_plural = _('transacciones')
        indexes = [
            models.Index(fields=['wallet', 'created_at'], name='wallet_tx_wallet_created_idx'),
            models.Index(fields=['status', 'type'], name='wallet_tx_status_type_idx'),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name='wallet_transaction_amount_positive'),
            # The gateway webhooks look a transaction up by its external id.
            models.UniqueConstraint(
                fields=['gateway', 'external_transaction_id'],
                condition=~models.Q(external_transaction_id=''),
                name='wallet_transaction_external_id_unique',
            ),
        ]

    def __str__(self):
        return f'{self.type} - {self.amount} ({self.status})'