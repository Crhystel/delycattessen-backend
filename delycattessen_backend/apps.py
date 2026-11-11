from django.contrib.admin.apps import AdminConfig


class DelycattessenAdminConfig(AdminConfig):
    """Makes the grouped, Spanish superadmin (core.admin_site) the default admin site."""

    default_site = 'core.admin_site.DelycattessenAdminSite'
