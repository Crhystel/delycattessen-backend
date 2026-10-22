import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Schema only: adds the nullable column. The table is already empty
    (0007), so the next migration can make it required without a default."""

    dependencies = [
        ('catalog', '0007_clear_legacy_menu_items'),
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
    ]
