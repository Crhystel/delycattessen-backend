"""Removes everything the k6 stress test created (SCRUM-26). Test environments only.

Run it inside the API container (PowerShell):
    Get-Content pruebas-carga/limpiar_carga.py | docker compose exec -T api python manage.py shell

It only touches the sede "Carga (prueba de estres)" and users named `carga_*`.
It refuses to run when that sede holds anything else, and it is all-or-nothing:
any unexpected relation cancels the whole cleanup.
"""
from django.db import transaction

from catalog.models import Ingredient, MenuDish, MenuItem, MonthlyMenu
from pos.models import PreOrder, Sale
from users.models import CustomUser, Institution, ParentProfile, StudentProfile
from wallet.models import Transaction, Wallet

SEDE_NAME = 'Carga (prueba de estres)'
PREFIX = 'carga_'

sede = Institution.objects.filter(name=SEDE_NAME).first()
if sede is None:
    print(f'Nothing to clean: the sede "{SEDE_NAME}" does not exist.')
else:
    foreign_users = CustomUser.objects.filter(institution=sede).exclude(username__startswith=PREFIX).count()
    foreign_students = StudentProfile.objects.filter(institution=sede).exclude(user__username__startswith=PREFIX).count()
    foreign_sales = Sale.objects.filter(institution=sede).exclude(cashier__username__startswith=PREFIX).count()
    if foreign_users or foreign_students or foreign_sales:
        raise ValueError(
            f'Cleanup cancelled: the sede holds data that is not from the load test '
            f'({foreign_users} users, {foreign_students} students, {foreign_sales} sales).'
        )

    students = StudentProfile.objects.filter(institution=sede)
    removed = {}
    with transaction.atomic():
        removed['transactions'] = Transaction.objects.filter(wallet__student__in=students).delete()[0]
        removed['sales'] = Sale.objects.filter(institution=sede).delete()[0]
        removed['pre_orders'] = PreOrder.objects.filter(student__in=students).delete()[0]
        removed['wallets'] = Wallet.objects.filter(student__in=students).delete()[0]
        removed['students'] = students.delete()[0]
        removed['parents'] = ParentProfile.objects.filter(user__username__startswith=PREFIX).delete()[0]
        removed['users'] = CustomUser.objects.filter(username__startswith=PREFIX).delete()[0]
        removed['monthly_menus'] = MonthlyMenu.objects.filter(institution=sede).delete()[0]
        removed['dishes'] = MenuDish.objects.filter(institution=sede).delete()[0]
        removed['products'] = MenuItem.objects.filter(institution=sede).delete()[0]
        removed['ingredients'] = Ingredient.objects.filter(name='Carga ingrediente', menu_items__isnull=True).delete()[0]
        sede.delete()
    print('Load test data removed:', ', '.join(f'{name}={count}' for name, count in removed.items()))
