from django.db import models
from django.utils.translation import gettext_lazy as _

# Ctalog models (Products and allergens)

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
        if self.stock <= 0:
            self.is_visible = False
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name