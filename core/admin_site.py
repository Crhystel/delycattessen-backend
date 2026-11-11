from django.contrib.admin import AdminSite
from django.urls import reverse
from django.utils.text import slugify


class DelycattessenAdminSite(AdminSite):
    """Superadmin grouped by what each table is for, in Spanish.

    It names models by (app_label, model name) strings and imports no app, so
    `core` keeps depending on nothing. A registered model that is not listed in
    SECTIONS still shows up, under "Otros", so it can never be hidden by mistake.
    """

    site_header = "D'Elycattessen · Administración"
    site_title = "D'Elycattessen"
    index_title = 'Panel del superadministrador'

    SECTIONS = (
        ('Personas y cuentas', (
            ('users', 'customuser'),
            ('users', 'parentprofile'),
            ('users', 'studentprofile'),
            ('users', 'parentalcontrol'),
            ('users', 'userbiometric'),
            ('users', 'institution'),
        )),
        ('Billetera y ventas (solo lectura)', (
            ('wallet', 'wallet'),
            ('wallet', 'transaction'),
            ('pos', 'sale'),
            ('pos', 'preorder'),
            ('pos', 'creditstatement'),
            ('pos', 'reportexport'),
        )),
        ('Catálogo y menús (solo lectura)', (
            ('catalog', 'allergen'),
            ('catalog', 'ingredient'),
            ('catalog', 'menuitem'),
            ('catalog', 'menudish'),
            ('catalog', 'monthlymenu'),
            ('catalog', 'promotion'),
            ('catalog', 'combo'),
        )),
        ('Seguridad y sistema', (
            ('admin', 'logentry'),
            ('axes', 'accessattempt'),  # deleting a row here unlocks a blocked login
        )),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The bulk "delete selected" action would bypass every protection.
        self.disable_action('delete_selected')

    def _section(self, title, models):
        slug = slugify(title)
        return {
            'name': title,
            'app_label': slug,
            'app_url': f"{reverse('admin:index', current_app=self.name)}#{slug}",
            'has_module_perms': True,
            'models': models,
        }

    def get_app_list(self, request, app_label=None):
        app_list = super().get_app_list(request, app_label)
        if app_label is not None:
            return app_list  # the per-app pages keep working as usual

        by_key = {
            (app['app_label'], model['object_name'].lower()): model
            for app in app_list
            for model in app['models']
        }
        sections, used = [], set()
        for title, keys in self.SECTIONS:
            present = [key for key in keys if key in by_key]
            used.update(present)
            if present:
                sections.append(self._section(title, [by_key[key] for key in present]))

        leftovers = sorted((m for key, m in by_key.items() if key not in used), key=lambda m: str(m['name']))
        if leftovers:
            sections.append(self._section('Otros', leftovers))
        return sections
