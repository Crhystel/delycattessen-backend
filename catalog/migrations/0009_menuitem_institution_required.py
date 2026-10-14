import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Schema only: makes institution required. Relies on 0007 having removed
    every row without a sede."""

    dependencies = [
        ('catalog', '0008_menuitem_institution'),
    ]

    operations = [
        migrations.AlterField(
            model_name='menuitem',
            name='institution',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='menu_items',
                to='users.institution',
                verbose_name='institution',
            ),
        ),
    ]
