from django.contrib import admin
from django.contrib.admin.models import LogEntry

from .admin_mixins import ReadOnlyAdminMixin


@admin.register(LogEntry)
class LogEntryAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Audit trail of what the superadmin did, for review only."""

    list_display = ['action_time', 'user', 'content_type', 'object_repr', 'action_flag']
    list_filter = ['action_flag', 'content_type']
    search_fields = ['object_repr', 'change_message', 'user__username']
    date_hierarchy = 'action_time'
    list_select_related = ['user', 'content_type']
