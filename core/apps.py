from django.apps import AppConfig
from django.contrib import admin
from django.contrib.admin.sites import NotRegistered


class CoreConfig(AppConfig):
    name = 'core'

    def ready(self):
        # Runs after the admin has discovered every admin.py, so these third-party
        # registrations already exist. They are internal bookkeeping (task results,
        # refresh-token lists, permission groups) that only confuses a superadmin.
        from axes.models import AccessFailureLog, AccessLog
        from django.contrib.auth.models import Group
        from django_celery_results.models import GroupResult, TaskResult
        from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

        from .admin_mixins import ReadOnlyAdminMixin

        noise = (Group, TaskResult, GroupResult, OutstandingToken, BlacklistedToken, AccessFailureLog, AccessLog)
        for model in noise:
            try:
                admin.site.unregister(model)
            except NotRegistered:
                pass

        # The login history of the panel stays, but as a record that cannot be edited or erased.
        @admin.register(AccessLog, site=admin.site)
        class AccessLogAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
            list_display = ('attempt_time', 'username', 'ip_address', 'logout_time', 'http_accept')
            list_filter = ('attempt_time',)
            search_fields = ('username', 'ip_address')
            date_hierarchy = 'attempt_time'
