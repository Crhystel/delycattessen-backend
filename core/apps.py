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

        noise = (Group, TaskResult, GroupResult, OutstandingToken, BlacklistedToken, AccessLog, AccessFailureLog)
        for model in noise:
            try:
                admin.site.unregister(model)
            except NotRegistered:
                pass
