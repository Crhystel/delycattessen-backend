from django.db import migrations


def resync_product_allergens(apps, schema_editor):
    """Products and dishes store a copy of their ingredients' allergens, but
    editing an ingredient used not to update it. Recomputes every stored copy
    so the POS and preorders block allergens added to an ingredient before."""
    for model_name in ('MenuItem', 'MenuDish'):
        model = apps.get_model('catalog', model_name)
        for obj in model.objects.all():
            allergen_ids = set(
                obj.ingredients.filter(allergens__isnull=False).values_list('allergens__id', flat=True)
            )
            obj.allergens.set(allergen_ids)


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0011_combo'),
    ]

    operations = [
        migrations.RunPython(resync_product_allergens, migrations.RunPython.noop),
    ]
