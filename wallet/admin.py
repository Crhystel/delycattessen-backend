"""Wallets and their transactions, for review only: the balance is moved exclusively
by credit_wallet / debit_wallet (wallet/services.py), which keep it equal to the history."""
from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from core.admin_mixins import ReadOnlyAdminMixin, ReadOnlyInlineMixin

from .models import Transaction, Wallet


class TransactionInline(ReadOnlyInlineMixin, admin.TabularInline):
    model = Transaction
    fields = ('created_at', 'type', 'amount', 'status', 'gateway', 'external_transaction_id', 'sale', 'pre_order')
    readonly_fields = fields
    ordering = ('-created_at',)


@admin.register(Wallet)
class WalletAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('student_username', 'balance', 'created_at', 'updated_at')
    search_fields = ('student__user__username', 'student__user__first_name', 'student__user__last_name')
    list_select_related = ('student__user',)
    inlines = [TransactionInline]

    @admin.display(description=_('Estudiante'), ordering='student__user__username')
    def student_username(self, obj):
        return obj.student.user.username


@admin.register(Transaction)
class TransactionAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('created_at', 'student_username', 'type', 'amount', 'status', 'gateway', 'external_transaction_id')
    list_filter = ('type', 'status', 'gateway')
    search_fields = ('external_transaction_id', 'wallet__student__user__username')
    date_hierarchy = 'created_at'
    list_select_related = ('wallet__student__user',)

    @admin.display(description=_('Estudiante'), ordering='wallet__student__user__username')
    def student_username(self, obj):
        return obj.wallet.student.user.username
