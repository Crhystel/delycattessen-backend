"""Sales, pre-orders and the monthly credit records, for review only: they are the
income history that the analytics and the HR reports are built from."""
from django.contrib import admin

from core.admin_mixins import ReadOnlyAdminMixin, ReadOnlyInlineMixin

from .models import CreditStatement, PreOrder, PreOrderItem, ReportExport, Sale, SaleCombo, SaleItem


class SaleItemInline(ReadOnlyInlineMixin, admin.TabularInline):
    model = SaleItem
    fields = ('menu_item', 'quantity', 'price_at_purchase')
    readonly_fields = fields


class SaleComboInline(ReadOnlyInlineMixin, admin.TabularInline):
    model = SaleCombo
    fields = ('name', 'quantity', 'price_at_purchase')
    readonly_fields = fields


class PreOrderItemInline(ReadOnlyInlineMixin, admin.TabularInline):
    model = PreOrderItem
    fields = ('dish', 'quantity', 'price_at_purchase')
    readonly_fields = fields


@admin.register(Sale)
class SaleAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('id', 'created_at', 'institution', 'payment_method', 'total_amount', 'cashier', 'customer')
    list_filter = ('payment_method', 'institution')
    search_fields = ('cashier__username', 'customer__username', 'customer__email')
    date_hierarchy = 'created_at'
    list_select_related = ('institution', 'cashier', 'customer')
    inlines = [SaleItemInline, SaleComboInline]


@admin.register(PreOrder)
class PreOrderAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('id', 'created_at', 'student', 'daily_menu', 'status', 'total_amount')
    list_filter = ('status',)
    search_fields = ('student__user__username',)
    date_hierarchy = 'created_at'
    list_select_related = ('student__user', 'daily_menu')
    inlines = [PreOrderItemInline]


@admin.register(CreditStatement)
class CreditStatementAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('month', 'teacher', 'institution', 'total', 'status', 'sent_at')
    list_filter = ('status', 'institution', 'month')
    search_fields = ('teacher__username', 'teacher__email')
    list_select_related = ('teacher', 'institution')


@admin.register(ReportExport)
class ReportExportAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('id', 'created_at', 'requested_by', 'institution', 'month', 'file_type', 'status')
    list_filter = ('status', 'file_type')
    search_fields = ('requested_by__username',)
    list_select_related = ('requested_by', 'institution')
