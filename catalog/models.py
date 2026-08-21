from django.db import models
from django.utils.translation import gettext_lazy as _

# Modelos del catálogo (Productos y Alérgenos)

class Allergen(models.Model):
    name = models.CharField(_('name'), max_length=100, unique=True)
    description = models.TextField(_('description'), blank=True)

    class Meta:
        verbose_name = _('allergen')
        verbose_name_plural = _('allergens')

    def __str__(self):
        return self.name

class MenuItem(models.Model):
    name = models.CharField(_('name'), max_length=200)
    description = models.TextField(_('description'), blank=True)
    price = models.DecimalField(_('price'), max_digits=6, decimal_places=2)
    is_active = models.BooleanField(_('is active'), default=True)
    allergens = models.ManyToManyField(Allergen, blank=True, related_name='menu_items')
    # image = models.ImageField(upload_to='menu/', blank=True, null=True)

    class Meta:
        verbose_name = _('menu item')
        verbose_name_plural = _('menu items')

    def __str__(self):
        return self.name
