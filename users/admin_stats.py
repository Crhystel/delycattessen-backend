"""Per-sede figures for the superadmin sede list.

One grouped query per metric, joined by sede id in Python: the cost is a handful of
queries however many sedes there are, and money is never summed over a join (which
would multiply it). `wallet` and `pos` sit above `users` in the dependency order, so
their models are resolved lazily instead of imported.
"""
from collections import defaultdict
from decimal import Decimal

from django.apps import apps
from django.db.models import Count, Max, Sum

from core.timezone import local_now

from .models import CustomUser, StudentProfile


def _month_start():
    return local_now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def institution_stats():
    """{institution_id: {staff, students, balance, month_sales, month_count, last_sale}}"""
    stats = defaultdict(lambda: {
        'staff': 0, 'students': 0, 'balance': Decimal('0.00'),
        'month_sales': Decimal('0.00'), 'month_count': 0, 'last_sale': None,
    })

    staff = (
        CustomUser.objects.filter(institution__isnull=False, deleted_at__isnull=True, is_active=True)
        .values('institution_id').annotate(total=Count('id'))
    )
    for row in staff:
        stats[row['institution_id']]['staff'] = row['total']

    students = (
        StudentProfile.objects.filter(user__deleted_at__isnull=True)
        .values('institution_id').annotate(total=Count('id'))
    )
    for row in students:
        stats[row['institution_id']]['students'] = row['total']

    Wallet = apps.get_model('wallet', 'Wallet')
    for row in (
        Wallet.objects.filter(student__user__deleted_at__isnull=True)
        .values('student__institution_id').annotate(total=Sum('balance'))
    ):
        stats[row['student__institution_id']]['balance'] = row['total'] or Decimal('0.00')

    Sale = apps.get_model('pos', 'Sale')
    for row in (
        Sale.objects.filter(created_at__gte=_month_start())
        .values('institution_id').annotate(total=Sum('total_amount'), count=Count('id'), last=Max('created_at'))
    ):
        entry = stats[row['institution_id']]
        entry['month_sales'] = row['total'] or Decimal('0.00')
        entry['month_count'] = row['count']
        entry['last_sale'] = row['last']
    return stats
