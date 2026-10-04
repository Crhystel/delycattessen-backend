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
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name