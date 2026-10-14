from django.db import migrations


def delete_legacy_menu_items(apps, schema_editor):
    """Legacy products were global (no institution), so they cannot be
    assigned to a sede and are removed. Data only, in its own migration:
    PostgreSQL cannot alter a table that still has deferred FK trigger events
    from a DELETE in the same transaction. Safe to re-run: with no rows left
    it does nothing. MenuDish/MonthlyMenu/DailyMenu are untouched."""
    MenuItem = apps.get_model('catalog', 'MenuItem')
    MenuItem.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0006_menudish_monthlymenu_dailymenu_and_more'),
    ]

    operations = [
        # Irreversible data removal: reversing leaves the table as it is.
        migrations.RunPython(delete_legacy_menu_items, migrations.RunPython.noop),
    ]
