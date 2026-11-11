from django.apps import AppConfig


class WalletConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'wallet'

    def ready(self):
        from . import alerts  # noqa: F401 - registers the superadmin alert providers
