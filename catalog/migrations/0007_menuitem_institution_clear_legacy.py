import django.db.models.deletion
from django.db import migrations, models


def delete_legacy_menu_items(apps, schema_editor):
    """Legacy products were global (no institution), so they cannot be
    assigned to a sede. They are removed; MenuDish/MonthlyMenu are untouched."""
    MenuItem = apps.get_model('catalog', 'MenuItem')
    MenuItem.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0006_menudish_monthlymenu_dailymenu_and_more'),
        ('users', '0008_remove_userallergy_allergen_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='menuitem',
            name='institution',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='menu_items',
                to='users.institution',
                verbose_name='institution',
            ),
        ),
        migrations.RunPython(delete_legacy_menu_items, migrations.RunPython.noop),
    ]
