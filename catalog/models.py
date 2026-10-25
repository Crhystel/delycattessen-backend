from decimal import Decimal
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _


class Allergen(models.Model):
    name = models.CharField(_('name'), max_length=100, unique=True)
    description = models.TextField(_('description'), blank=True)

    class Meta:
        verbose_name = _('allergen')
        verbose_name_plural = _('allergens')

    def __str__(self):
        return self.name

class Ingredient(models.Model):
    name = models.CharField(_('name'), max_length=100, unique=True)
    description = models.TextField(_('description'), blank=True)
    allergens = models.ManyToManyField(Allergen, blank=True, related_name='ingredients')
    allergens_reviewed = models.BooleanField(
        _('allergens reviewed'),
        default=False,
        help_text=_(
            'True once an admin has explicitly confirmed this ingredient\'s '
            'allergens (even confirming it has none). Stays False for '
            'newly created ingredients until someone reviews it — this is '
            'what MenuItemSerializer checks before allowing a product to '
            'be saved with this ingredient.'
        ),
    )

    class Meta:
        verbose_name = _('ingredient')
        verbose_name_plural = _('ingredients')

    def __str__(self):
        return self.name

class MenuItem(models.Model):
    institution = models.ForeignKey(
        'users.Institution',
        verbose_name=_('institution'),
        on_delete=models.PROTECT,
        related_name='menu_items',
    )
    name = models.CharField(_('name'), max_length=200)
    description = models.TextField(_('description'), blank=True)
    category = models.CharField(_('category'), max_length=100, blank=True)
    image = models.ImageField(_('image'), upload_to='catalog/menu_items/', blank=True, null=True)
    price = models.DecimalField(_('price'), max_digits=6, decimal_places=2)
    is_active = models.BooleanField(_('is active'), default=True)
    is_visible = models.BooleanField(_('is visible'), default=True)
    stock = models.IntegerField(_('stock'), default=0)
    ingredients = models.ManyToManyField(Ingredient, related_name='menu_items')
    allergens = models.ManyToManyField(Allergen, blank=True, related_name='menu_items')

    class Meta:
        verbose_name = _('menu item')
        verbose_name_plural = _('menu items')

    def save(self, *args, **kwargs):
        self.is_visible = self.stock > 0
        if self.image:
            from delycattessen_backend.image_utils import resize_image_field
            resize_image_field(self.image)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class MenuDish(models.Model):
    """
    Monthly menu dish belonging to a specific institution.
    Dishes have no inventory stock (prepared to order based on preorders).
    """
    institution = models.ForeignKey(
        'users.Institution',
        verbose_name=_('institution'),
        on_delete=models.PROTECT,
        related_name='menu_dishes',
    )
    name = models.CharField(_('name'), max_length=200)
    description = models.TextField(_('description'), blank=True)
    price = models.DecimalField(
        _('price'),
        max_digits=6,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
    )
    image = models.ImageField(
        _('image'),
        upload_to='catalog/menu_dishes/',
        blank=False,
        null=False,
    )
    ingredients = models.ManyToManyField(Ingredient, related_name='menu_dishes')
    allergens = models.ManyToManyField(Allergen, blank=True, related_name='menu_dishes')
    is_active = models.BooleanField(_('is active'), default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('menu dish')
        verbose_name_plural = _('menu dishes')

    def save(self, *args, **kwargs):
        if self.image:
            from delycattessen_backend.image_utils import resize_image_field
            resize_image_field(self.image)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.institution.name})"


class MonthlyMenu(models.Model):
    """
    Monthly menu planning per institution and month.
    Created in DRAFT status, publishing notifies parents asynchronously.
    """
    class Status(models.TextChoices):
        DRAFT = 'DRAFT', _('Borrador')
        PUBLISHED = 'PUBLISHED', _('Publicado')

    institution = models.ForeignKey(
        'users.Institution',
        verbose_name=_('institution'),
        on_delete=models.PROTECT,
        related_name='monthly_menus',
    )
    year = models.PositiveSmallIntegerField(_('year'))
    month = models.PositiveSmallIntegerField(
        _('month'),
        validators=[MinValueValidator(1), MaxValueValidator(12)],
    )
    status = models.CharField(
        _('status'),
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    published_at = models.DateTimeField(_('published at'), null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_('created by'),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_monthly_menus',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('monthly menu')
        verbose_name_plural = _('monthly menus')
        constraints = [
            models.UniqueConstraint(
                fields=['institution', 'year', 'month'],
                name='unique_monthly_menu_per_institution_month',
            )
        ]

    def __str__(self):
        return f"{self.institution.name} - {self.year}/{self.month:02d} ({self.get_status_display()})"


class DailyMenu(models.Model):
    """
    Business day menu within a published or draft monthly menu.
    """
    monthly_menu = models.ForeignKey(
        MonthlyMenu,
        verbose_name=_('monthly menu'),
        on_delete=models.CASCADE,
        related_name='days',
    )
    date = models.DateField(_('date'))
    dishes = models.ManyToManyField(MenuDish, blank=True, related_name='daily_menus')

    class Meta:
        verbose_name = _('daily menu')
        verbose_name_plural = _('daily menus')
        ordering = ['date']
        constraints = [
            models.UniqueConstraint(
                fields=['monthly_menu', 'date'],
                name='unique_daily_menu_per_month_and_date',
            )
        ]

    def __str__(self):
        return f"{self.monthly_menu} - {self.date}"


class ValidityQuerySet(models.QuerySet):
    def in_force(self, on_date):
        """Offers whose validity period includes `on_date`."""
        return self.filter(start_date__lte=on_date, end_date__gte=on_date)


class ValidityPeriod(models.Model):
    """
    Abstract base for catalog offers valid between two dates (discounts and
    combos). The status is never stored: it is derived from the date, so an
    offer stops applying on its own once end_date passes.
    """
    class Status(models.TextChoices):
        SCHEDULED = 'SCHEDULED', _('Programada')
        ACTIVE = 'ACTIVE', _('Activa')
        EXPIRED = 'EXPIRED', _('Vencida')

    start_date = models.DateField(_('start date'))
    end_date = models.DateField(_('end date'))

    objects = ValidityQuerySet.as_manager()

    class Meta:
        abstract = True

    def status_on(self, on_date):
        if on_date < self.start_date:
            return self.Status.SCHEDULED
        if on_date > self.end_date:
            return self.Status.EXPIRED
        return self.Status.ACTIVE


class Promotion(ValidityPeriod):
    """
    Percentage discount over catalog products (HU-03.3) for a validity period.
    The product price is never modified: the discounted price is derived
    while the promotion is in force, so the original price comes back on its
    own once end_date passes, with no manual step or scheduled job.
    Monthly menu dishes are out of scope: only MenuItem can be promoted.
    """
    institution = models.ForeignKey(
        'users.Institution',
        verbose_name=_('institution'),
        on_delete=models.PROTECT,
        related_name='promotions',
    )
    products = models.ManyToManyField(MenuItem, related_name='promotions')
    discount_percentage = models.PositiveSmallIntegerField(
        _('discount percentage'),
        validators=[MinValueValidator(1), MaxValueValidator(99)],
    )
    image = models.ImageField(_('image'), upload_to='catalog/promotions/', blank=True, null=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_('created by'),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_promotions',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('promotion')
        verbose_name_plural = _('promotions')
        ordering = ['-start_date', '-id']

    def save(self, *args, **kwargs):
        if self.image:
            from delycattessen_backend.image_utils import resize_image_field
            resize_image_field(self.image)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"-{self.discount_percentage}% ({self.start_date} - {self.end_date})"


class Combo(ValidityPeriod):
    """
    Fixed-price bundle of catalog products (e.g. chicken sandwich + cola for
    $3.50) for a validity period. It is shown as a banner and sold at the POS
    as a single line; each product's stock is still discounted.
    """
    institution = models.ForeignKey(
        'users.Institution',
        verbose_name=_('institution'),
        on_delete=models.PROTECT,
        related_name='combos',
    )
    name = models.CharField(_('name'), max_length=200)
    price = models.DecimalField(
        _('price'),
        max_digits=6,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
    )
    image = models.ImageField(_('image'), upload_to='catalog/combos/')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_('created by'),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_combos',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('combo')
        verbose_name_plural = _('combos')
        ordering = ['-start_date', '-id']

    def save(self, *args, **kwargs):
        if self.image:
            from delycattessen_backend.image_utils import resize_image_field
            resize_image_field(self.image)
        super().save(*args, **kwargs)

    def regular_price(self):
        """What the products would cost bought separately at their own price."""
        return sum(
            (item.menu_item.price * item.quantity for item in self.items.all()),
            Decimal('0.00'),
        )

    def __str__(self):
        return self.name


class ComboItem(models.Model):
    combo = models.ForeignKey(Combo, verbose_name=_('combo'), on_delete=models.CASCADE, related_name='items')
    menu_item = models.ForeignKey(
        MenuItem, verbose_name=_('product'), on_delete=models.PROTECT, related_name='combo_items',
    )
    quantity = models.PositiveSmallIntegerField(_('quantity'), default=1, validators=[MinValueValidator(1)])

    class Meta:
        verbose_name = _('combo item')
        verbose_name_plural = _('combo items')
        constraints = [
            models.UniqueConstraint(fields=['combo', 'menu_item'], name='unique_product_per_combo'),
        ]

    def __str__(self):
        return f'{self.quantity}x {self.menu_item.name}'
