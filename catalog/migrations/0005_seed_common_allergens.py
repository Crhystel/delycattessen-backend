from django.db import migrations


COMMON_ALLERGENS = [
    ("Gluten", "Presente en trigo, cebada, centeno y sus derivados."),
    ("Lácteos", "Leche de vaca y derivados (queso, yogurt, mantequilla, etc.)."),
    ("Huevo", "Clara y/o yema de huevo, presente también en productos horneados."),
    ("Maní", "Cacahuate y productos derivados o procesados con maní."),
    ("Frutos secos", "Nueces, almendras, avellanas, pistachos, anacardos, etc."),
    ("Soya", "Soya y sus derivados (salsa de soya, tofu, lecitina de soya)."),
    ("Pescado", "Cualquier tipo de pescado y sus derivados."),
    ("Mariscos", "Camarón, langosta, cangrejo y otros crustáceos."),
    ("Moluscos", "Almeja, mejillón, calamar, pulpo y otros moluscos."),
    ("Sésamo", "Ajonjolí y aceite o pasta derivada de sésamo."),
    ("Apio", "Apio y productos que lo contengan como ingrediente o especia."),
    ("Mostaza", "Semilla de mostaza y productos derivados."),
    ("Sulfitos", "Conservantes sulfurosos usados en frutas secas, vinos y otros."),
    ("Altramuces", "Lupino y harina de lupino, común en panificación."),
]


def seed_allergens(apps, schema_editor):
    Allergen = apps.get_model('catalog', 'Allergen')
    for name, description in COMMON_ALLERGENS:
        Allergen.objects.get_or_create(
            name__iexact=name,
            defaults={'name': name, 'description': description},
        )


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0004_ingredient_allergens_reviewed'),
    ]

    operations = [
        migrations.RunPython(seed_allergens, migrations.RunPython.noop),
    ]