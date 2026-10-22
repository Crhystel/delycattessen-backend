from django.db import migrations

ALLERGENS = ['Maní', 'Frutos secos', 'Lácteos', 'Huevo', 'Mariscos', 'Gluten', 'Soya', 'Pescado']


def seed_allergens(apps, schema_editor):
    Allergen = apps.get_model('users', 'Allergen')
    for name in ALLERGENS:
        Allergen.objects.get_or_create(name=name)


def remove_allergens(apps, schema_editor):
    Allergen = apps.get_model('users', 'Allergen')
    Allergen.objects.filter(name__in=ALLERGENS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('users', '0003_allergen_userallergy'),  
    ]
    operations = [
        migrations.RunPython(seed_allergens, remove_allergens),
    ]