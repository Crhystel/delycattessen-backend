from django.db import migrations


def delete_legacy_preorders(apps, schema_editor):
    PreOrder = apps.get_model('pos', 'PreOrder')
    PreOrder.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('pos', '0002_preorder_daily_menu_preorderitem_dish'),
    ]

    operations = [
        migrations.RunPython(delete_legacy_preorders, reverse_code=migrations.RunPython.noop),
    ]
