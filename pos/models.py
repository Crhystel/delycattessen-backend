from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _
from users.models import StudentProfile


class PreOrder(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', _('Pending')
        DELIVERED = 'DELIVERED', _('Delivered')
        CANCELED = 'CANCELED', _('Canceled')

    # PROTECT: pre-orders are part of the income history, never deleted with the student.
    student = models.ForeignKey(StudentProfile, on_delete=models.PROTECT, related_name='pre_orders')
    daily_menu = models.ForeignKey(
        'catalog.DailyMenu',
        on_delete=models.PROTECT,
        related_name='pre_orders',
    )
    total_amount = models.DecimalField(_('total amount'), max_digits=8, decimal_places=2)
    status = models.CharField(_('status'), max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('preorden')
        verbose_name_plural = _('preórdenes')
        indexes = [
            models.Index(fields=['created_at']),
            models.Index(fields=['status'], name='pos_preorder_status_idx'),
        ]

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

    class Meta:
        verbose_name = _('línea de preorden')
        verbose_name_plural = _('líneas de preorden')
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gte=1), name='pos_preorderitem_quantity_min_1'),
        ]

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
        verbose_name = _('venta')
        verbose_name_plural = _('ventas')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['customer', 'created_at']),
            models.Index(fields=['institution', 'created_at']),
            models.Index(fields=['payment_method'], name='pos_sale_payment_method_idx'),
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

    class Meta:
        verbose_name = _('línea de venta')
        verbose_name_plural = _('líneas de venta')
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gte=1), name='pos_saleitem_quantity_min_1'),
        ]

    def __str__(self):
        return f'{self.quantity}x {self.menu_item.name}'


class SaleCombo(models.Model):
    """A combo sold as one line. The name is kept so the receipt survives if
    the admin later deletes the combo."""
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name='combos')
    combo = models.ForeignKey(
        'catalog.Combo', on_delete=models.SET_NULL, null=True, blank=True, related_name='sale_lines',
    )
    name = models.CharField(_('name'), max_length=200)
    quantity = models.PositiveIntegerField()
    price_at_purchase = models.DecimalField(_('price at purchase'), max_digits=6, decimal_places=2)

    class Meta:
        verbose_name = _('combo vendido')
        verbose_name_plural = _('combos vendidos')
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gte=1), name='pos_salecombo_quantity_min_1'),
        ]

    def __str__(self):
        return f'{self.quantity}x {self.name}'


class ReportExport(models.Model):
    """A report file (Excel or PDF) generated in the background (Celery) for the administrator."""

    class Status(models.TextChoices):
        PENDING = 'PENDING', _('En cola')
        PROCESSING = 'PROCESSING', _('Generando')
        DONE = 'DONE', _('Listo')
        FAILED = 'FAILED', _('Fallido')

    class FileType(models.TextChoices):
        XLSX = 'xlsx', 'Excel'
        PDF = 'pdf', 'PDF'

    ACTIVE_STATUSES = (Status.PENDING, Status.PROCESSING)

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_('requested by'),
        on_delete=models.CASCADE, related_name='report_exports',
    )
    institution = models.ForeignKey(
        'users.Institution', verbose_name=_('institution'), on_delete=models.CASCADE,
        related_name='report_exports', null=True, blank=True,
        help_text=_('Empty for the consolidated view (every institution).'),
    )
    month = models.CharField(
        _('month'), max_length=7, blank=True,
        help_text=_('YYYY-MM. Empty for the current month (day 1 until the moment it is generated).'),
    )
    file_type = models.CharField(_('file type'), max_length=4, choices=FileType.choices, default=FileType.XLSX)
    status = models.CharField(_('status'), max_length=12, choices=Status.choices, default=Status.PENDING)
    file = models.FileField(_('file'), upload_to='exports/', blank=True)
    error = models.CharField(_('error'), max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _('reporte exportado')
        verbose_name_plural = _('reportes exportados')
        indexes = [models.Index(fields=['requested_by', 'status'])]

    def __str__(self):
        return f'ReportExport {self.id} ({self.status})'


class CreditStatement(models.Model):
    """The credit statement of one closed month emailed to a teacher (one per teacher,
    institution and month). It records that it was sent, so a double click never mails twice."""

    class Status(models.TextChoices):
        QUEUED = 'QUEUED', _('En cola')
        SENT = 'SENT', _('Enviado')
        FAILED = 'FAILED', _('Fallido')

    teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_('teacher'),
        on_delete=models.PROTECT, related_name='credit_statements',
    )
    institution = models.ForeignKey(
        'users.Institution', verbose_name=_('institution'),
        on_delete=models.PROTECT, related_name='credit_statements',
    )
    month = models.CharField(_('month'), max_length=7, help_text=_('YYYY-MM'))
    total = models.DecimalField(_('total'), max_digits=10, decimal_places=2, default=0)
    status = models.CharField(_('status'), max_length=8, choices=Status.choices, default=Status.QUEUED)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_('sent by'),
        on_delete=models.PROTECT, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _('estado de cuenta')
        verbose_name_plural = _('estados de cuenta')
        constraints = [
            models.UniqueConstraint(fields=['teacher', 'institution', 'month'], name='unique_credit_statement'),
        ]

    def __str__(self):
        return f'CreditStatement {self.month} teacher={self.teacher_id} ({self.status})'
