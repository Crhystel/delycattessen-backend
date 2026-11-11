from django.apps import AppConfig


class PosConfig(AppConfig):
    name = 'pos'

    def ready(self):
        from . import alerts  # noqa: F401 - registers the superadmin alert providers
