"""Seeds the data of the k6 stress test (SCRUM-26). Test environments only.

Run it inside the API container (PowerShell):
    Get-Content pruebas-carga/seed_carga.py | docker compose exec -T api python manage.py shell

Everything lives in its own sede ("Carga") and every user starts with `carga_`.
Idempotent: running it again resets balances and stock and refreshes the menus,
so run it again on the day of the test (the menus cover the current and next month).
"""
import os
from datetime import timedelta
from decimal import Decimal

from decouple import Config, RepositoryEnv
from django.utils import timezone

from catalog.models import Ingredient, MenuDish, MenuItem, MonthlyMenu
from catalog.services import MonthlyMenuService
from core.timezone import local_today
from users.models import CustomUser, Institution, ParentProfile, StudentProfile
from wallet.models import Wallet


def _setting(name, default=None):
    """Environment first, then the .env that is mounted in the container."""
    if name in os.environ:
        return os.environ[name]
    try:
        return Config(RepositoryEnv('.env'))(name, default=default)
    except FileNotFoundError:
        return default


PASSWORD = _setting('LOAD_PASSWORD')
if not PASSWORD:
    raise ValueError('LOAD_PASSWORD is not set in .env')
CASHIERS = int(_setting('LOAD_CASHIERS', 25))
STUDENTS = int(_setting('LOAD_STUDENTS', 50))
# Every parent needs at least one child to have something to look at.
PARENTS = min(int(_setting('LOAD_PARENTS', 25)), STUDENTS)
TEACHERS = int(_setting('LOAD_TEACHERS', 12))
PRODUCTS = 5
DISHES = 3
# Large enough that a long run never runs out of money or stock (it would show up as false 400s).
HIGH_BALANCE = Decimal('900000.00')
HIGH_STOCK = 1_000_000


def _account(username, email, role, institution=None):
    user, created = CustomUser.objects.get_or_create(
        username=username,
        defaults={'email': email, 'role': role, 'institution': institution},
    )
    if created or not user.check_password(PASSWORD):
        user.set_password(PASSWORD)
    user.is_active = True
    user.must_change_password = False
    user.save()
    return user


institution, _created = Institution.objects.get_or_create(name='Carga (prueba de estres)')
admin = _account('carga_admin', 'carga_admin@carga.test', CustomUser.Role.ADMIN, institution)

for number in range(1, CASHIERS + 1):
    _account(
        f'carga_cajero_{number:02d}', f'carga_cajero_{number:02d}@carga.test',
        CustomUser.Role.OPERATIONS_STAFF, institution,
    )

for number in range(1, TEACHERS + 1):
    _account(
        f'carga_docente_{number:02d}', f'carga_docente_{number:02d}@carga.test',
        CustomUser.Role.TEACHER, institution,
    )

parents = []
for number in range(1, PARENTS + 1):
    parent_user = _account(f'carga_padre_{number:02d}', f'carga_padre_{number:02d}@carga.test', CustomUser.Role.PARENT)
    parent, _created = ParentProfile.objects.get_or_create(user=parent_user)
    parents.append(parent)

for number in range(1, STUDENTS + 1):
    student_user = _account(f'carga_estudiante_{number:02d}', None, CustomUser.Role.STUDENT)
    profile, _created = StudentProfile.objects.get_or_create(
        user=student_user, defaults={'institution': institution, 'parent': parents[0]},
    )
    # Children are spread over the parents (also fixes the single parent of older seeds).
    StudentProfile.objects.filter(pk=profile.pk).update(parent=parents[(number - 1) % len(parents)])
    wallet, _created = Wallet.objects.get_or_create(
        student=profile, defaults={'low_balance_threshold': Decimal('2.00')},
    )
    Wallet.objects.filter(pk=wallet.pk).update(balance=HIGH_BALANCE)

ingredient, _created = Ingredient.objects.get_or_create(
    name='Carga ingrediente', defaults={'allergens_reviewed': True},
)
for number in range(1, PRODUCTS + 1):
    item, _created = MenuItem.objects.get_or_create(
        institution=institution, name=f'Carga producto {number}',
        defaults={'price': Decimal('0.75') + Decimal('0.25') * number},
    )
    item.ingredients.add(ingredient)
    # update() skips save(), which would recompute is_visible from the stock.
    MenuItem.objects.filter(pk=item.pk).update(stock=HIGH_STOCK, is_active=True, is_visible=True)

# Published monthly menus (current and next month) so parents can pre-order.
dishes = []
for number in range(1, DISHES + 1):
    dish, _created = MenuDish.objects.get_or_create(
        institution=institution, name=f'Carga plato {number}',
        defaults={'price': Decimal('1.50') + Decimal('0.50') * number},
    )
    MenuDish.objects.filter(pk=dish.pk).update(is_active=True)
    dishes.append(dish)

today = local_today()
next_month = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
for year, month in {(today.year, today.month), (next_month.year, next_month.month)}:
    menu = MonthlyMenu.objects.filter(institution=institution, year=year, month=month).first()
    if menu is None:
        menu = MonthlyMenuService.create_month(institution, year, month, admin)
    for day in menu.days.all():
        day.dishes.set(dishes)
    # Published straight in the database: publishing through the API sends e-mails to the parents.
    MonthlyMenu.objects.filter(pk=menu.pk).update(status=MonthlyMenu.Status.PUBLISHED, published_at=timezone.now())

print(
    f'Carga seed ready: sede "{institution.name}", {CASHIERS} cashiers, {TEACHERS} teachers, '
    f'{PARENTS} parents, {STUDENTS} students with ${HIGH_BALANCE} each, {PRODUCTS} products, {DISHES} dishes.'
)
