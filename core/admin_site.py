from django.contrib.admin import AdminSite
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.text import slugify

from . import alerts, health


class DelycattessenAdminSite(AdminSite):
    """Superadmin grouped by what each table is for, in Spanish.

    It names models by (app_label, model name) strings and imports no app, so
    `core` keeps depending on nothing. A registered model that is not listed in
    SECTIONS still shows up, under "Otros", so it can never be hidden by mistake.
    The index also shows the alerts and links to the system status page.
    """

    site_header = "D'Elycattessen · Administración"
    site_title = "D'Elycattessen"
    index_title = 'Panel del superadministrador'
    index_template = 'admin/delycattessen_index.html'

    SECTIONS = (
        ('Personas y cuentas', (
            ('users', 'customuser'),
            ('users', 'parentprofile'),
            ('users', 'studentprofile'),
            ('users', 'institution'),
        )),
        ('Seguridad y sistema', (
            ('users', 'auditlog'),   # who did what, from the app, the superadmin and the system
            ('users', 'termsacceptance'),  # legal evidence of each acceptance of the terms
            ('admin', 'logentry'),   # changes made in this panel
            ('axes', 'accesslog'),   # logins to this panel
            ('axes', 'accessattempt'),  # deleting a row here unlocks a blocked login
        )),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The bulk "delete selected" action would bypass every protection.
        self.disable_action('delete_selected')

    # ------------------------------------------------------------------ urls
    def get_urls(self):
        custom = [
            path('estado-del-sistema/', self.admin_view(self.system_status_view), name='system_status'),
        ]
        return custom + super().get_urls()

    def system_status_view(self, request):
        # SMTP logs in to the mail server: only when the button is pressed.
        checks = health.run_checks(include_smtp=request.method == 'POST' and 'check_smtp' in request.POST)
        alerts.collect_alerts(force=True)  # the index shows the problems just found
        context = {
            **self.each_context(request),
            'title': 'Estado del sistema',
            'checks': checks,
            'overall': health.overall_status(checks),
            'smtp_checked': any(check.name.startswith('Correo') for check in checks),
        }
        return TemplateResponse(request, 'admin/system_status.html', context)

    def index(self, request, extra_context=None):
        extra_context = {
            **(extra_context or {}),
            'alerts': alerts.collect_alerts(),
            'system_status_url': reverse('admin:system_status', current_app=self.name),
        }
        return super().index(request, extra_context)

    # ------------------------------------------------------------- app list
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
