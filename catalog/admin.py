"""Catalog and menus, for review only. Editing is done from the web panel because the
API keeps invariants that the admin would skip: the allergens stored on every product and
dish follow their ingredients (catalog/services.py), and a deleted allergen would
silently remove it from students' allergies."""
from django.contrib import admin

from core.admin_mixins import ReadOnlyAdminMixin, ReadOnlyInlineMixin

from .models import Allergen, Combo, ComboItem, Ingredient, MenuDish, MenuItem, MonthlyMenu, Promotion


class ComboItemInline(ReadOnlyInlineMixin, admin.TabularInline):
    model = ComboItem
    fields = ('menu_item', 'quantity')
    readonly_fields = fields


@admin.register(Allergen)
class AllergenAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('name', 'description')
    search_fields = ('name',)


@admin.register(Ingredient)
class IngredientAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('name', 'allergens_reviewed')
    list_filter = ('allergens_reviewed',)
    search_fields = ('name',)


@admin.register(MenuItem)
class MenuItemAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('name', 'institution', 'category', 'price', 'stock', 'is_visible', 'is_active')
    list_filter = ('institution', 'category', 'is_visible', 'is_active')
    search_fields = ('name',)
    list_select_related = ('institution',)


@admin.register(MenuDish)
class MenuDishAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('name', 'institution', 'price', 'is_active')
    list_filter = ('institution', 'is_active')
    search_fields = ('name',)
    list_select_related = ('institution',)


@admin.register(MonthlyMenu)
class MonthlyMenuAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('institution', 'year', 'month', 'status', 'published_at')
    list_filter = ('institution', 'status', 'year')
    list_select_related = ('institution',)


@admin.register(Promotion)
class PromotionAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('discount_percentage', 'institution', 'start_date', 'end_date')
    list_filter = ('institution',)
    list_select_related = ('institution',)


@admin.register(Combo)
class ComboAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('name', 'institution', 'price', 'start_date', 'end_date')
    list_filter = ('institution',)
    search_fields = ('name',)
    list_select_related = ('institution',)
    inlines = [ComboItemInline]
