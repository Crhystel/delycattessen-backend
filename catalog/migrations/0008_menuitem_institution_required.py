import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0007_menuitem_institution_clear_legacy'),
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
