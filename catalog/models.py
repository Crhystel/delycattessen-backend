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