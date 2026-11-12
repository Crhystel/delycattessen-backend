import logging
import os
import tempfile
import uuid
from collections import namedtuple
from datetime import datetime, time, timedelta
from decimal import Decimal
from itertools import groupby
from types import SimpleNamespace
from xml.sax.saxutils import escape

import xlsxwriter
from django.conf import settings
from django.core.cache import cache
from django.core.files import File
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Sum
from django.db.models.functions import TruncDate, TruncMonth, TruncWeek
from django.template.loader import render_to_string
from django.utils.formats import date_format
from django.utils.text import slugify
from django.utils.translation import gettext, gettext_lazy as _
from kombu.exceptions import OperationalError
from rest_framework.exceptions import PermissionDenied, ValidationError

from catalog.models import DailyMenu, MenuItem, MonthlyMenu
from catalog.services import ComboService, LowStockService
from core.timezone import LOCAL_TIMEZONE, local_now, local_today
from users.models import CustomUser, StudentProfile
from users.permissions import get_object_institution_id
from wallet.services import debit_wallet

from .mixins import AllergenValidatorMixin, ParentalControlValidatorMixin
from .models import CreditStatement, PreOrder, PreOrderItem, ReportExport, Sale, SaleCombo, SaleItem
from .tasks import build_teacher_credit_export_task, send_credit_statement_email

logger = logging.getLogger(__name__)


class SaleProcessor(AllergenValidatorMixin):
    """
    Template Method: process() fixes the sale flow (lock products -> validate
    customer -> register sale -> charge -> discount stock). Subclasses only
    change who can be the customer and how the sale is charged. Combos are
    one line at their fixed price, but their products are locked, checked for
    allergens and discounted from stock like the rest.
    """
    payment_method = None
    allowed_roles = ()
    customer_required = True
    role_error = _('Este cliente no puede usar este método de pago.')

    def __init__(self, institution, cashier, customer=None):
        self.institution = institution
        self.cashier = cashier
        self.customer = customer

    @transaction.atomic
    def process(self, lines, combo_lines=()):
        """`lines` is a list of (menu_item_id, quantity); `combo_lines` of (combo_id, quantity)."""
        combos = ComboService.combos_for_sale(self.institution, combo_lines)
        locked = self._lock_items(list(lines) + [
            (combo_item.menu_item_id, combo_item.quantity * quantity)
            for combo, quantity in combos
            for combo_item in combo.items.all()
        ])
        items = self._standalone_items(lines, locked)
        total = sum((item.price * quantity for item, quantity in items), Decimal('0.00'))
        total += sum((combo.price * quantity for combo, quantity in combos), Decimal('0.00'))

        self.validate_customer(locked, total)

        sale = Sale.objects.create(
            institution=self.institution,
            cashier=self.cashier,
            customer=self.customer,
            payment_method=self.payment_method,
            total_amount=total,
        )
        SaleItem.objects.bulk_create([
            SaleItem(sale=sale, menu_item=item, quantity=quantity, price_at_purchase=item.price)
            for item, quantity in items
        ])
        SaleCombo.objects.bulk_create([
            SaleCombo(sale=sale, combo=combo, name=combo.name, quantity=quantity, price_at_purchase=combo.price)
            for combo, quantity in combos
        ])
        self.charge(sale)
        self._discount_stock(locked)
        return sale

    def validate_customer(self, items, total):
        if self.customer is None:
            if self.customer_required:
                raise ValidationError({'user_id': _('Este método de pago requiere identificar al cliente.')})
            return
        if self.customer.role not in self.allowed_roles:
            raise PermissionDenied(self.role_error)
        if not self.customer.is_active or get_object_institution_id(self.customer) != self.institution.id:
            raise ValidationError({'user_id': _('El cliente no pertenece a esta institución.')})
        self.validate_allergens(self.allergy_owner(), [item for item, _quantity in items])

    def allergy_owner(self):
        """Object whose `allergies` are checked against the products."""
        return self.customer

    def charge(self, sale):
        """Money movement; nothing to do unless the payment method moves money."""

    def _lock_items(self, lines):
        quantities = {}
        for menu_item_id, quantity in lines:
            quantities[menu_item_id] = quantities.get(menu_item_id, 0) + quantity

        found = {
            item.id: item
            for item in MenuItem.objects.select_for_update().filter(
                id__in=sorted(quantities), institution=self.institution, is_active=True,
            ).order_by('id')
        }
        if len(found) != len(quantities):
            raise ValidationError({'items': _('Uno o más productos no existen en esta institución.')})

        items = []
        for menu_item_id, quantity in quantities.items():
            item = found[menu_item_id]
            if item.stock < quantity:
                raise ValidationError({
                    'items': _('Stock insuficiente de "%(name)s".') % {'name': item.name}
                })
            items.append((item, quantity))
        return items

    @staticmethod
    def _standalone_items(lines, locked):
        """Products bought on their own (outside combos), as (item, quantity)."""
        quantities = {}
        for menu_item_id, quantity in lines:
            quantities[menu_item_id] = quantities.get(menu_item_id, 0) + quantity
        by_id = {item.id: item for item, _quantity in locked}
        return [(by_id[menu_item_id], quantity) for menu_item_id, quantity in quantities.items()]

    def _discount_stock(self, items):
        changes = {}
        for item, quantity in items:
            new_stock = item.stock - quantity
            MenuItem.objects.filter(pk=item.pk).update(stock=new_stock, is_visible=new_stock > 0)
            changes[item.pk] = (item.stock, new_stock)
        LowStockService.check(self.institution, changes)


class CashSaleProcessor(SaleProcessor):
    """Cash payment. Without a customer it is a quick sale."""
    payment_method = Sale.PaymentMethod.CASH
    allowed_roles = (CustomUser.Role.STUDENT, CustomUser.Role.TEACHER)
    customer_required = False

    def allergy_owner(self):
        return getattr(self.customer, 'student_profile', None) or self.customer


class CreditSaleProcessor(SaleProcessor):
    """Institutional credit: teachers only. The consumption stays on their name."""
    payment_method = Sale.PaymentMethod.CREDIT
    allowed_roles = (CustomUser.Role.TEACHER,)
    role_error = _('Solo los docentes pueden comprar a crédito.')


class WalletSaleProcessor(ParentalControlValidatorMixin, SaleProcessor):
    """Wallet balance: students only, subject to parental controls."""
    payment_method = Sale.PaymentMethod.WALLET
    allowed_roles = (CustomUser.Role.STUDENT,)
    role_error = _('Solo los estudiantes pueden pagar con saldo.')

    def allergy_owner(self):
        student = getattr(self.customer, 'student_profile', None)
        if student is None:
            raise ValidationError({'user_id': _('El cliente no tiene un perfil de estudiante.')})
        return student

    def validate_customer(self, items, total):
        super().validate_customer(items, total)
        self.validate_parental_controls(self.customer.student_profile, total)

    def charge(self, sale):
        debit_wallet(self.customer.student_profile.wallet, sale.total_amount, sale=sale)


PROCESSORS = {
    Sale.PaymentMethod.CASH: CashSaleProcessor,
    Sale.PaymentMethod.CREDIT: CreditSaleProcessor,
    Sale.PaymentMethod.WALLET: WalletSaleProcessor,
}


def process_sale(payment_method, institution, cashier, customer, lines, combo_lines=()):
    return PROCESSORS[payment_method](institution, cashier, customer).process(lines, combo_lines)


def month_start_local(now=None):
    """First instant of the current month in the institution's timezone."""
    now = now or local_now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def full_name(user):
    """The four name parts that are filled in, in order."""
    parts = (user.first_name, user.second_name, user.last_name, user.second_last_name)
    return ' '.join(part for part in parts if part).strip() or user.username


def credit_period(month=None, now=None):
    """
    (start, end) of the teachers' credit report, in local time. `month` is
    "YYYY-MM" (None = the current month). The current month runs from day 1
    until `now`; a past month is complete, up to its last instant; a month that
    has not started is rejected.
    """
    now = (now or local_now()).astimezone(LOCAL_TIMEZONE)
    current = month_start_local(now)
    if month:
        try:
            year, number = (int(part) for part in month.split('-'))
            start = datetime(year, number, 1, tzinfo=LOCAL_TIMEZONE)
        except ValueError:
            raise ValidationError({'month': _('El mes debe tener el formato AAAA-MM.')})
    else:
        start = current
    if start > current:
        raise ValidationError({'month': _('No se puede consultar un mes que aún no empieza.')})
    if start == current:
        return start, now
    next_start = (start + timedelta(days=32)).replace(day=1)
    return start, next_start - timedelta(microseconds=1)


def teacher_credit_sales(institution, period):
    """Credit sales inside `period` = (start, end) (None = every institution)."""
    start, end = period
    sales = Sale.objects.filter(
        payment_method=Sale.PaymentMethod.CREDIT, created_at__gte=start, created_at__lte=end,
    )
    return sales if institution is None else sales.filter(institution=institution)


def teacher_credit_report(institution, month=None, now=None):
    """
    Credit consumptions of the teachers in a month (HU-08.2): the current one
    from day 1 until `now`, or a complete past one (`month` = "YYYY-MM"). One
    row per teacher and institution with each sale, plus the totals.
    `institution=None` consolidates every institution. Teachers deactivated
    later still appear: their consumption happened.
    """
    generated_at = now or local_now()
    start, end = credit_period(month, generated_at)
    sales = teacher_credit_sales(institution, (start, end)).select_related(
        'customer', 'institution').prefetch_related('items__menu_item', 'combos')

    rows = {}
    for sale in sales.order_by('created_at'):
        row = rows.setdefault((sale.customer_id, sale.institution_id), {
            'teacher': sale.customer,
            'full_name': full_name(sale.customer),
            'institution': sale.institution,
            'sales': [],
            'total': Decimal('0.00'),
        })
        row['sales'].append(sale)
        row['total'] += sale.total_amount
    statements = {
        (statement.teacher_id, statement.institution_id): statement
        for statement in CreditStatement.objects.filter(month=f'{start:%Y-%m}', teacher_id__in={key[0] for key in rows})
    }
    for key, row in rows.items():
        row['statement'] = statements.get(key)
    teachers = sorted(rows.values(), key=lambda row: (row['full_name'].casefold(), row['institution'].name))
    return {
        'date_from': start.date(),
        'date_to': end.date(),
        'generated_at': generated_at,
        'teachers': teachers,
        'totals': {
            'teachers': len({row['teacher'].pk for row in teachers}),
            'consumptions': sum(len(row['sales']) for row in teachers),
            'amount': sum((row['total'] for row in teachers), Decimal('0.00')),
        },
    }


TEACHER_ORDER = (
    'customer__first_name', 'customer__second_name', 'customer__last_name', 'customer__second_last_name',
    'customer_id', 'institution__name', 'institution_id',
)


def teacher_credit_summary(institution, period):
    """One aggregated row per teacher and institution (consumptions and total) of the credit sales in
    `period`: the single query the report files and the statement emails start from."""
    return list(
        teacher_credit_sales(institution, period)
        .values(
            'customer_id', 'institution_id', 'institution__name', 'customer__username', 'customer__email',
            'customer__first_name', 'customer__second_name', 'customer__last_name',
            'customer__second_last_name',
        )
        .annotate(consumptions=Count('id'), total=Sum('total_amount'))
        .order_by(*TEACHER_ORDER)
    )


class TeacherCreditReportBuilder:
    """
    Data and brand shared by the files of the teachers' credit report (HU-08.2).
    A subclass only writes the file (`build(path)`); a new format is one more
    subclass registered in EXPORT_FORMATS. Every format reads the same two
    queries: one aggregated row per teacher and institution, and the sales
    streamed in teacher order (iterator + prefetch), so memory stays flat
    however many sales there are.
    """
    ITERATOR_CHUNK = 2000
    INK, BLUE, DARK_BLUE, LIGHT_BLUE = '#0C1B2A', '#007ACC', '#005C99', '#E5F3FF'
    GOLD, LIGHT_GOLD, ACCENT = '#A87028', '#FEF8EC', '#F5B551'

    def __init__(self, institution, institution_label, period, generated_at, generated_by=''):
        self.institution = institution
        self.institution_label = institution_label
        self.period = period
        self.start, self.end = period
        self.generated_at = generated_at
        self.generated_by = generated_by
        self.period_label = f'{self.start:%d/%m/%Y} - {self.end:%d/%m/%Y}'

    def summary_rows(self):
        return teacher_credit_summary(self.institution, self.period)

    def sales(self):
        return (
            teacher_credit_sales(self.institution, self.period)
            .select_related('customer', 'institution').prefetch_related('items__menu_item', 'combos')
            .order_by(*TEACHER_ORDER, 'created_at', 'id')
            .iterator(chunk_size=self.ITERATOR_CHUNK)
        )

    @staticmethod
    def teacher_name(row):
        return full_name(SimpleNamespace(
            first_name=row['customer__first_name'], second_name=row['customer__second_name'],
            last_name=row['customer__last_name'], second_last_name=row['customer__second_last_name'],
            username=row['customer__username'],
        ))

    @staticmethod
    def surnames_and_names(row):
        """(surnames, names) of a summary row, the way payroll systems ask for them."""
        surnames = ' '.join(part for part in (row['customer__last_name'], row['customer__second_last_name']) if part)
        names = ' '.join(part for part in (row['customer__first_name'], row['customer__second_name']) if part)
        return surnames, names or row['customer__username']

    @staticmethod
    def sale_description(sale):
        """What was bought with its unit price, e.g. "2 × Sandwich ($2.50 c/u), 1 × Combo ($3.50 c/u)"."""
        lines = [f'{item.quantity} × {item.menu_item.name} (${item.price_at_purchase} c/u)' for item in sale.items.all()]
        lines += [f'{combo.quantity} × {combo.name} (${combo.price_at_purchase} c/u)' for combo in sale.combos.all()]
        return ', '.join(lines)

    @staticmethod
    def sale_units(sale):
        return sum(item.quantity for item in sale.items.all()) + sum(combo.quantity for combo in sale.combos.all())


class TeacherCreditExcelBuilder(TeacherCreditReportBuilder):
    """
    Writes the report as a styled .xlsx for Human Resources: "Resumen" (one row
    per teacher and institution, to read), "Nómina" (the same rows as a flat
    table for the payroll system: headers on row 1, no titles, merged cells or
    totals) and "Detalle" (one row per sale, with its description, units and
    amount). It streams (xlsxwriter constant_memory). Text is always written as
    a string, so a name like "=cmd()" is never a formula.
    """
    SUMMARY_SHEET = 'Resumen'
    PAYROLL_SHEET = 'Nómina'
    DETAIL_SHEET = 'Detalle'
    FONT = 'Segoe UI'
    LABEL_FILL, LINE = '#EAE1F7', '#BFBFBF'
    MONEY = '"$"#,##0.00'
    TITLE_HEIGHT, KPI_HEIGHT, HEADER_HEIGHT, ROW_HEIGHT = 36, 24.6, 24, 22.05

    def build(self, path):
        workbook = xlsxwriter.Workbook(path, {'constant_memory': True})
        workbook.set_properties({'title': gettext('Consumo a crédito docente'), 'company': "D'Elycattessen"})
        self.workbook = workbook
        self._formats = {}
        rows = self.summary_rows()
        self._write_summary(workbook.add_worksheet(self.SUMMARY_SHEET), rows)
        self._write_payroll(workbook.add_worksheet(self.PAYROLL_SHEET), rows)
        self._write_detail(workbook.add_worksheet(self.DETAIL_SHEET))
        workbook.close()

    def _fmt(self, **props):
        """Cell format with the report's base look (font, thin border, centered vertically), cached by properties."""
        key = tuple(sorted(props.items()))
        if key not in self._formats:
            base = {'font_name': self.FONT, 'font_size': 9, 'font_color': self.INK, 'valign': 'vcenter',
                    'border': 1, 'border_color': self.LINE}
            self._formats[key] = self.workbook.add_format({**base, **props})
        return self._formats[key]

    def _title(self, sheet, last_col, text):
        sheet.set_row(1, self.TITLE_HEIGHT)
        sheet.merge_range(1, 1, 1, last_col, text, self._fmt(
            bold=True, font_size=13, font_color='#FFFFFF', bg_color=self.DARK_BLUE, align='center'))

    def _meta(self, sheet, row, col, label, value):
        sheet.write_string(row, col, label, self._fmt(bold=True, bg_color=self.LABEL_FILL))
        sheet.write_string(row, col + 1, value, self._fmt())

    def _headers(self, sheet, row, headers, first_col=1):
        sheet.set_row(row, self.HEADER_HEIGHT)
        for offset, (text, align) in enumerate(headers):
            sheet.write_string(row, first_col + offset, text, self._fmt(
                bold=True, font_size=10, font_color='#FFFFFF', bg_color=self.BLUE, align=align))

    def _total_cell(self, sheet, row, col, first, last, value, money):
        letter = xlsxwriter.utility.xl_col_to_name(col)
        formula = f'=SUM({letter}{first}:{letter}{last})' if last >= first else '=0'
        fmt = self._fmt(bold=True, bg_color=self.LIGHT_GOLD, num_format=self.MONEY if money else '#,##0',
                        align='right' if money else 'center')
        sheet.write_formula(row, col, formula, fmt, float(value))

    def _total_label(self, sheet, row, last_label_col):
        fmt = self._fmt(bold=True, bg_color=self.LIGHT_GOLD, align='left')
        sheet.set_row(row, self.HEADER_HEIGHT)
        sheet.merge_range(row, 1, row, last_label_col, gettext('Total general'), fmt)

    def _write_summary(self, sheet, rows):
        total_amount = sum((row['total'] for row in rows), Decimal('0.00'))
        total_sales = sum(row['consumptions'] for row in rows)

        for col, width in enumerate((3, 28, 26, 18, 16, 16)):
            sheet.set_column(col, col, width)
        sheet.set_landscape()
        sheet.set_paper(9)
        sheet.fit_to_pages(1, 0)
        self._title(sheet, 5, gettext('REPORTE DE CONSUMOS A CRÉDITO DOCENTE'))
        self._meta(sheet, 3, 1, gettext('Sede:'), self.institution_label)
        self._meta(sheet, 3, 4, gettext('Generado:'), f'{self.generated_at:%d/%m/%Y %H:%M}')
        self._meta(sheet, 4, 1, gettext('Periodo:'), self.period_label)
        self._meta(sheet, 4, 4, gettext('Generado por:'), self.generated_by)

        blue = {'bold': True, 'font_size': 8, 'font_color': self.DARK_BLUE, 'bg_color': self.LIGHT_BLUE,
                'align': 'center'}
        gold = {'bold': True, 'font_size': 8, 'font_color': self.GOLD, 'bg_color': self.LIGHT_GOLD, 'align': 'center'}
        sheet.write_string(6, 1, gettext('DOCENTES'), self._fmt(**blue))
        sheet.write_string(6, 2, gettext('CONSUMOS'), self._fmt(**blue))
        sheet.merge_range(6, 3, 6, 4, gettext('TOTAL A CONSOLIDAR'), self._fmt(**gold))
        sheet.set_row(7, self.KPI_HEIGHT)
        value = {'bold': True, 'font_size': 16, 'align': 'center'}
        teachers = len({row['customer_id'] for row in rows})
        sheet.write_number(7, 1, teachers, self._fmt(**value, bg_color=self.LIGHT_BLUE, num_format='#,##0'))
        sheet.write_number(7, 2, total_sales, self._fmt(**value, bg_color=self.LIGHT_BLUE, num_format='#,##0'))
        sheet.merge_range(7, 3, 7, 4, float(total_amount), self._fmt(
            **value, bg_color=self.LIGHT_GOLD, num_format=self.MONEY))

        header_row = 9
        self._headers(sheet, header_row, (
            (gettext('Docente'), 'left'), (gettext('Correo Electrónico'), 'left'), (gettext('Sede'), 'left'),
            (gettext('N.º Consumos'), 'center'), (gettext('Total'), 'center'),
        ))
        for index, row in enumerate(rows):
            line = header_row + 1 + index
            sheet.set_row(line, self.ROW_HEIGHT)
            sheet.write_string(line, 1, self.teacher_name(row), self._fmt())
            sheet.write_string(line, 2, row['customer__email'] or '', self._fmt())
            sheet.write_string(line, 3, row['institution__name'], self._fmt())
            sheet.write_number(line, 4, row['consumptions'], self._fmt(align='center'))
            sheet.write_number(line, 5, float(row['total']), self._fmt(num_format=self.MONEY, align='right'))
        first, last = header_row + 2, header_row + 1 + len(rows)
        total_row = last
        self._total_label(sheet, total_row, 3)
        self._total_cell(sheet, total_row, 4, first, last, total_sales, money=False)
        self._total_cell(sheet, total_row, 5, first, last, total_amount, money=True)

    def _write_payroll(self, sheet, rows):
        """Flat table to import into the payroll system (ERP): one record per teacher and institution."""
        for col, width in enumerate((10, 24, 24, 30, 18, 30, 12, 12, 13, 17)):
            sheet.set_column(col, col, width)
        sheet.freeze_panes(1, 0)
        headers = (
            (gettext('Código'), 'center'), (gettext('Apellidos'), 'left'), (gettext('Nombres'), 'left'),
            (gettext('Correo'), 'left'), (gettext('Sede'), 'left'), (gettext('Concepto'), 'left'),
            (gettext('Desde'), 'center'), (gettext('Hasta'), 'center'), (gettext('N.º consumos'), 'center'),
            (gettext('Valor a descontar'), 'right'),
        )
        self._headers(sheet, 0, headers, first_col=0)
        concept = gettext('Consumo cafetería a crédito')
        for index, row in enumerate(rows):
            line = 1 + index
            surnames, names = self.surnames_and_names(row)
            sheet.write_number(line, 0, row['customer_id'], self._fmt(align='center'))
            sheet.write_string(line, 1, surnames, self._fmt())
            sheet.write_string(line, 2, names, self._fmt())
            sheet.write_string(line, 3, row['customer__email'] or '', self._fmt())
            sheet.write_string(line, 4, row['institution__name'], self._fmt())
            sheet.write_string(line, 5, concept, self._fmt())
            sheet.write_datetime(line, 6, self.start.date(), self._fmt(num_format='dd/mm/yyyy', align='center'))
            sheet.write_datetime(line, 7, self.end.date(), self._fmt(num_format='dd/mm/yyyy', align='center'))
            sheet.write_number(line, 8, row['consumptions'], self._fmt(align='center'))
            sheet.write_number(line, 9, float(row['total']), self._fmt(num_format='0.00', align='right'))
        sheet.autofilter(0, 0, max(len(rows), 1), len(headers) - 1)

    def _write_detail(self, sheet):
        for col, width in enumerate((3, 14, 28, 26, 18, 12, 8, 46, 9, 15)):
            sheet.set_column(col, col, width)
        sheet.set_landscape()
        sheet.set_paper(9)
        sheet.fit_to_pages(1, 0)
        self._title(sheet, 9, gettext('DETALLE DE CONSUMOS A CRÉDITO DOCENTE'))
        self._meta(sheet, 3, 1, gettext('Sede:'), self.institution_label)
        self._meta(sheet, 4, 1, gettext('Periodo:'), self.period_label)
        header_row = 6
        self._headers(sheet, header_row, (
            (gettext('N.º comprobante'), 'center'), (gettext('Docente'), 'left'), (gettext('Correo'), 'left'),
            (gettext('Sede'), 'left'), (gettext('Fecha'), 'center'), (gettext('Hora'), 'center'),
            (gettext('Detalle (cantidad × producto y precio c/u)'), 'left'), (gettext('Ítems'), 'center'),
            (gettext('Monto'), 'right'),
        ))
        count, total = 0, Decimal('0.00')
        for sale in self.sales():
            line = header_row + 1 + count
            when = sale.created_at.astimezone(LOCAL_TIMEZONE)
            sheet.set_row(line, self.ROW_HEIGHT)
            sheet.write_number(line, 1, sale.pk, self._fmt(align='center'))
            sheet.write_string(line, 2, full_name(sale.customer), self._fmt())
            sheet.write_string(line, 3, sale.customer.email or '', self._fmt())
            sheet.write_string(line, 4, sale.institution.name, self._fmt())
            sheet.write_datetime(line, 5, when.date(), self._fmt(num_format='dd/mm/yyyy', align='center'))
            sheet.write_datetime(line, 6, when.time().replace(second=0, microsecond=0),
                                 self._fmt(num_format='hh:mm', align='center'))
            sheet.write_string(line, 7, self.sale_description(sale), self._fmt())
            sheet.write_number(line, 8, self.sale_units(sale), self._fmt(align='center'))
            sheet.write_number(line, 9, float(sale.total_amount), self._fmt(num_format=self.MONEY, align='right'))
            count += 1
            total += sale.total_amount
        first, last = header_row + 2, header_row + 1 + count
        self._total_label(sheet, last, 8)
        self._total_cell(sheet, last, 9, first, last, total, money=True)


class TeacherCreditPdfBuilder(TeacherCreditReportBuilder):
    """
    Writes the report as an A4 landscape PDF for Human Resources: header with
    the brand, summary per teacher, detail per teacher with subtotals, grand
    total and signature lines. Built to stay light on large months: reportlab is
    imported here (only the Celery worker loads it), sales are streamed and
    grouped one teacher at a time, each teacher gets its own small table and
    cells are plain text (clipped to their column) instead of Paragraphs.
    """
    MARGIN_MM = 14
    FIRST_BAND_MM, LATER_BAND_MM, ACCENT_MM = 24, 13, 1.5
    NAME_LIMIT, EMAIL_LIMIT, DESCRIPTION_LIMIT = 52, 46, 90
    GRID = '#BFBFBF'

    def build(self, path):
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.units import mm
        from reportlab.platypus import BaseDocTemplate, Frame, NextPageTemplate, PageTemplate, Paragraph, Spacer

        page_width, page_height = landscape(A4)
        margin = self.MARGIN_MM * mm
        self._geometry = (page_width, page_height, margin, page_width - 2 * margin)
        self._styles = self._make_styles()
        rows = self.summary_rows()

        def frame(band_mm, name):
            top = page_height - (band_mm + self.ACCENT_MM + 5) * mm
            return Frame(margin, 16 * mm, page_width - 2 * margin, top - 16 * mm, id=name,
                         leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)

        doc = BaseDocTemplate(
            path, pagesize=(page_width, page_height), title=gettext('Consumo a crédito docente'),
            author="D'Elycattessen", leftMargin=margin, rightMargin=margin, topMargin=margin, bottomMargin=margin,
        )
        doc.addPageTemplates([
            PageTemplate(id='first', frames=[frame(self.FIRST_BAND_MM, 'first')],
                         onPage=lambda canvas, _doc: self._draw_band(canvas, self.FIRST_BAND_MM, True)),
            PageTemplate(id='later', frames=[frame(self.LATER_BAND_MM, 'later')],
                         onPage=lambda canvas, _doc: self._draw_band(canvas, self.LATER_BAND_MM, False)),
        ])
        story = [NextPageTemplate('later'), *self._intro(rows), *self._summary(rows)]
        if rows:
            story.append(Spacer(1, 6 * mm))
            story.append(Paragraph(gettext('Detalle por docente'), self._styles['h2']))
            story.extend(self._detail())
        else:
            story.append(Paragraph(gettext('No hay consumos a crédito en el periodo.'), self._styles['body']))
        story.extend(self._signatures())
        doc.build(story, canvasmaker=self._numbered_canvas())

    # -- styles ---------------------------------------------------------------
    def _make_styles(self):
        from reportlab.lib import colors
        from reportlab.lib.styles import ParagraphStyle

        ink = colors.HexColor(self.INK)
        return {
            'h2': ParagraphStyle('h2', fontName='Helvetica-Bold', fontSize=11, leading=14, spaceAfter=3,
                                 textColor=colors.HexColor(self.DARK_BLUE), keepWithNext=1),
            'group': ParagraphStyle('group', fontName='Helvetica', fontSize=9, leading=12, textColor=ink,
                                    spaceBefore=7, spaceAfter=2, keepWithNext=1),
            'body': ParagraphStyle('body', fontName='Helvetica', fontSize=9, leading=12, textColor=ink),
        }

    def _table(self, data, widths, aligns, total_rows=(), header_rows=1):
        """Table with the report's look: blue header row, thin grid, gold total rows."""
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.platypus import Table, TableStyle

        last = len(data) - 1
        commands = [
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'), ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.HexColor(self.INK)),
            ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor(self.GRID)),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('BACKGROUND', (0, 0), (-1, header_rows - 1), colors.HexColor(self.BLUE)),
            ('TEXTCOLOR', (0, 0), (-1, header_rows - 1), colors.white),
            ('FONTNAME', (0, 0), (-1, header_rows - 1), 'Helvetica-Bold'),
        ]
        commands += [('ALIGN', (col, 0), (col, last), align) for col, align in enumerate(aligns)]
        for row in total_rows:
            commands += [('BACKGROUND', (0, row), (-1, row), colors.HexColor(self.LIGHT_GOLD)),
                         ('FONTNAME', (0, row), (-1, row), 'Helvetica-Bold'),
                         ('TEXTCOLOR', (0, row), (-1, row), colors.HexColor(self.GOLD))]
        return Table(data, colWidths=[width * mm for width in widths], repeatRows=header_rows,
                     style=TableStyle(commands))

    # -- content --------------------------------------------------------------
    def _intro(self, rows):
        """Report data and the three headline figures."""
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.platypus import Spacer, Table, TableStyle

        total = sum((row['total'] for row in rows), Decimal('0.00'))
        teachers = len({row['customer_id'] for row in rows})
        consumptions = sum(row['consumptions'] for row in rows)
        width = self._geometry[3]

        meta = Table([
            [gettext('Sede'), self._clip(self.institution_label, self.NAME_LIMIT), gettext('Periodo'), self.period_label],
            [gettext('Generado'), f'{self.generated_at:%d/%m/%Y %H:%M}', gettext('Generado por'),
             self._clip(self.generated_by, self.NAME_LIMIT)],
        ], colWidths=[28 * mm, 100 * mm, 28 * mm, width - 156 * mm], style=TableStyle([
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'), ('FONTSIZE', (0, 0), (-1, -1), 8.5),
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'), ('FONTNAME', (2, 0), (2, -1), 'Helvetica-Bold'),
            ('BACKGROUND', (0, 0), (0, -1), colors.HexColor(self.LIGHT_BLUE)),
            ('BACKGROUND', (2, 0), (2, -1), colors.HexColor(self.LIGHT_BLUE)),
            ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor(self.GRID)),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.HexColor(self.INK)),
            ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ]))
        third = width / 3
        kpis = Table([
            [gettext('DOCENTES'), gettext('CONSUMOS'), gettext('TOTAL A CONSOLIDAR')],
            [f'{teachers:,}', f'{consumptions:,}', self._money(total)],
        ], colWidths=[third, third, third], rowHeights=[6 * mm, 11 * mm], hAlign='LEFT', style=TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'), ('FONTSIZE', (0, 0), (-1, 0), 7.5),
            ('FONTSIZE', (0, 1), (-1, 1), 15),
            ('BACKGROUND', (0, 0), (1, -1), colors.HexColor(self.LIGHT_BLUE)),
            ('TEXTCOLOR', (0, 0), (1, -1), colors.HexColor(self.DARK_BLUE)),
            ('BACKGROUND', (2, 0), (2, -1), colors.HexColor(self.LIGHT_GOLD)),
            ('TEXTCOLOR', (2, 0), (2, -1), colors.HexColor(self.GOLD)),
            ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor(self.GRID)),
        ]))
        return [meta, Spacer(1, 4 * mm), kpis, Spacer(1, 6 * mm)]

    def _summary(self, rows):
        from reportlab.platypus import Paragraph

        data = [[gettext('Código'), gettext('Docente'), gettext('Correo'), gettext('Sede'),
                 gettext('N.º consumos'), gettext('Total')]]
        for row in rows:
            data.append([str(row['customer_id']), self._clip(self.teacher_name(row), self.NAME_LIMIT),
                         self._clip(row['customer__email'] or '', self.EMAIL_LIMIT),
                         self._clip(row['institution__name'], 30), str(row['consumptions']),
                         self._money(row['total'])])
        total = sum((row['total'] for row in rows), Decimal('0.00'))
        data.append(['', gettext('Total general'), '', '', str(sum(row['consumptions'] for row in rows)),
                     self._money(total)])
        table = self._table(data, (18, 76, 70, 45, 24, 36),
                            ('CENTER', 'LEFT', 'LEFT', 'LEFT', 'CENTER', 'RIGHT'), total_rows=(len(data) - 1,))
        return [Paragraph(gettext('Resumen por docente'), self._styles['h2']), table]

    def _detail(self):
        """One block per teacher and institution: heading, its sales and the subtotal."""
        from reportlab.platypus import Paragraph

        blocks = []
        for _key, group in groupby(self.sales(), key=lambda sale: (sale.customer_id, sale.institution_id)):
            sales = list(group)
            teacher, institution = sales[0].customer, sales[0].institution
            heading = f'<b>{escape(full_name(teacher))}</b> · {escape(institution.name)}'
            if teacher.email:
                heading += f' · {escape(teacher.email)}'
            data = [[gettext('Comprobante'), gettext('Fecha'), gettext('Hora'), gettext('Detalle'),
                     gettext('Ítems'), gettext('Monto')]]
            for sale in sales:
                when = sale.created_at.astimezone(LOCAL_TIMEZONE)
                data.append([str(sale.pk), f'{when:%d/%m/%Y}', f'{when:%H:%M}',
                             self._clip(self.sale_description(sale), self.DESCRIPTION_LIMIT),
                             str(self.sale_units(sale)), self._money(sale.total_amount)])
            subtotal = sum((sale.total_amount for sale in sales), Decimal('0.00'))
            data.append(['', '', '', gettext('Subtotal del docente'), str(sum(self.sale_units(s) for s in sales)),
                         self._money(subtotal)])
            blocks += [Paragraph(heading, self._styles['group']),
                       self._table(data, (28, 24, 16, 150, 16, 35),
                                   ('CENTER', 'CENTER', 'CENTER', 'LEFT', 'CENTER', 'RIGHT'),
                                   total_rows=(len(data) - 1,))]
        return blocks

    def _signatures(self):
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.platypus import KeepTogether, Spacer, Table, TableStyle

        width = self._geometry[3]
        table = Table([
            ['', '', ''],
            [gettext('Elaborado por: %(name)s') % {'name': self._clip(self.generated_by, 40)}, '',
             gettext('Revisado por: Recursos Humanos')],
        ], colWidths=[100 * mm, width - 200 * mm, 100 * mm], rowHeights=[16 * mm, None], style=TableStyle([
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'), ('FONTSIZE', (0, 0), (-1, -1), 8.5),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.HexColor(self.INK)),
            ('LINEABOVE', (0, 1), (0, 1), 0.6, colors.HexColor(self.INK)),
            ('LINEABOVE', (2, 1), (2, 1), 0.6, colors.HexColor(self.INK)),
            ('ALIGN', (0, 1), (-1, 1), 'CENTER'),
        ]))
        return [KeepTogether([Spacer(1, 8 * mm), table])]

    # -- page decoration ------------------------------------------------------
    def _draw_band(self, canvas, band_mm, first):
        """Blue band with the yellow accent line and the brand mark (same look as the emails)."""
        from reportlab.lib import colors
        from reportlab.lib.units import mm

        page_width, page_height, margin, _width = self._geometry
        band = band_mm * mm
        canvas.saveState()
        canvas.setFillColor(colors.HexColor(self.DARK_BLUE))
        canvas.rect(0, page_height - band, page_width, band, stroke=0, fill=1)
        canvas.setFillColor(colors.HexColor(self.ACCENT))
        canvas.rect(0, page_height - band - self.ACCENT_MM * mm, page_width, self.ACCENT_MM * mm, stroke=0, fill=1)
        size = 9 * mm
        middle = page_height - band / 2
        canvas.roundRect(margin, middle - size / 2, size, size, 2 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.HexColor(self.INK))
        canvas.setFont('Helvetica-Bold', 14)
        canvas.drawCentredString(margin + size / 2, middle - 2 * mm, 'D')
        canvas.setFillColor(colors.white)
        canvas.setFont('Helvetica-Bold', 14)
        canvas.drawString(margin + size + 3 * mm, middle - 2 * mm, "D'ELYCATTESSEN")
        canvas.setFont('Helvetica-Bold', 12)
        canvas.drawRightString(page_width - margin, middle + (1.5 if first else -1.5) * mm,
                               gettext('REPORTE DE CONSUMOS A CRÉDITO DOCENTE'))
        if first:
            canvas.setFont('Helvetica', 9)
            canvas.drawRightString(page_width - margin, middle - 5 * mm,
                                   gettext('Descuento por rol de pagos · Periodo: %(period)s') % {
                                       'period': self.period_label})
        canvas.restoreState()

    def _numbered_canvas(self):
        """Canvas that writes "Página X de Y" (it needs the total, so pages are drawn at the end)."""
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.pdfgen.canvas import Canvas

        page_width, _page_height, margin, _width = self._geometry
        right = page_width - margin

        class NumberedCanvas(Canvas):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._saved_pages = []

            def showPage(self):
                self._saved_pages.append(dict(self.__dict__))
                self._startPage()

            def save(self):
                total = len(self._saved_pages)
                for state in self._saved_pages:
                    self.__dict__.update(state)
                    self.setFillColor(colors.HexColor('#6B7B8A'))
                    self.setFont('Helvetica', 7)
                    self.drawString(margin, 8 * mm, gettext("D'Elycattessen · Documento confidencial para uso de Recursos Humanos"))
                    self.drawRightString(right, 8 * mm, gettext('Página %(page)s de %(total)s') % {
                        'page': self._pageNumber, 'total': total})
                    super().showPage()
                super().save()

        return NumberedCanvas

    # -- small helpers --------------------------------------------------------
    @staticmethod
    def _clip(text, limit):
        return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'

    @staticmethod
    def _money(value):
        return f'${value:,.2f}'


ExportFormat = namedtuple('ExportFormat', 'builder extension content_type')

# One entry per file type a report can be exported to (open/closed: a new format is a new builder here).
EXPORT_FORMATS = {
    ReportExport.FileType.XLSX: ExportFormat(
        TeacherCreditExcelBuilder, 'xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
    ReportExport.FileType.PDF: ExportFormat(TeacherCreditPdfBuilder, 'pdf', 'application/pdf'),
}


def purge_report_exports(now=None):
    """
    Fails exports nobody picked up (PENDING longer than the queue timeout: the
    Celery worker is down) or that never finished (stuck longer than the
    timeout), and deletes the old ones with their files. Runs whenever an
    export is requested or polled, so the web always ends up with an answer.
    """
    now = now or local_now()
    for statuses, seconds, message in (
        ((ReportExport.Status.PENDING,), settings.REPORT_EXPORT_QUEUE_TIMEOUT_SECONDS,
         gettext('El servicio de reportes no está disponible en este momento. Intenta de nuevo más tarde.')),
        (ReportExport.ACTIVE_STATUSES, settings.REPORT_EXPORT_TIMEOUT_SECONDS,
         gettext('El reporte tardó demasiado. Intenta de nuevo.')),
    ):
        ReportExport.objects.filter(status__in=statuses, created_at__lt=now - timedelta(seconds=seconds)).update(
            status=ReportExport.Status.FAILED, error=message, finished_at=now,
        )
    for export in ReportExport.objects.filter(created_at__lt=now - timedelta(days=settings.REPORT_EXPORT_TTL_DAYS)):
        export.file.delete(save=False)
        export.delete()


def start_teacher_credit_export(user, institution, month='', file_type=ReportExport.FileType.XLSX):
    """
    Queues the build of the teachers' credit report file (Excel or PDF, of the
    current month or a past one) and returns the export to poll. An unfinished
    export of the same user, scope, month and type is reused instead of queueing
    a duplicate (double clicks, several tabs).
    """
    credit_period(month or None)  # rejects a bad or future month before anything is queued
    purge_report_exports()
    request = {'requested_by': user, 'institution': institution, 'month': month or '', 'file_type': file_type}
    active = ReportExport.objects.filter(**request, status__in=ReportExport.ACTIVE_STATUSES).first()
    if active is not None:
        return active
    export = ReportExport.objects.create(**request)
    transaction.on_commit(lambda: build_teacher_credit_export_task.delay(export.pk))
    return export


def teacher_credit_export_filename(export):
    scope = slugify(export.institution.name) if export.institution else 'consolidado'
    start, end = credit_period(export.month or None, export.finished_at or local_now())
    return f'consumo-docentes-{scope}-{start:%Y-%m-%d}_{end:%Y-%m-%d}.{EXPORT_FORMATS[export.file_type].extension}'


def run_teacher_credit_export(export_id):
    """Celery task body: PENDING -> PROCESSING -> DONE, or FAILED with a user-safe message."""
    export = ReportExport.objects.select_related('institution', 'requested_by').filter(pk=export_id).first()
    if export is None or export.status != ReportExport.Status.PENDING:
        return
    export.status = ReportExport.Status.PROCESSING
    export.save(update_fields=['status'])
    path = None
    try:
        now = local_now()
        label = export.institution.name if export.institution else gettext('Todas las sedes')
        export_format = EXPORT_FORMATS[export.file_type]
        with tempfile.NamedTemporaryFile(suffix=f'.{export_format.extension}', delete=False) as handle:
            path = handle.name
        export_format.builder(
            export.institution, label, credit_period(export.month or None, now), now, full_name(export.requested_by),
        ).build(path)
        with open(path, 'rb') as built:
            export.file.save(f'{uuid.uuid4().hex}.{export_format.extension}', File(built), save=False)
        export.status, export.finished_at = ReportExport.Status.DONE, now
        export.save()
    except Exception:
        logger.exception('Teacher credit export %s failed', export_id)
        export.status = ReportExport.Status.FAILED
        export.error = gettext('No se pudo generar el reporte. Intenta de nuevo.')
        export.finished_at = local_now()
        export.save(update_fields=['status', 'error', 'finished_at'])
    finally:
        if path and os.path.exists(path):
            os.remove(path)


def closed_credit_month(month):
    """(start, end) of a month a credit statement may be sent for: it has to be over already, because the
    amount of a month in progress still changes. Raises ValidationError otherwise."""
    if not month:
        raise ValidationError({'month': _('Indica el mes del estado de cuenta (AAAA-MM).')})
    period = credit_period(month)  # also rejects a malformed or future month
    if period[0] >= month_start_local():
        raise ValidationError({'month': _('El estado de cuenta se envía cuando el mes cierra.')})
    return period


def send_credit_statements(admin, institution, month, resend=False):
    """
    Queues the credit statement email (one Celery task per teacher) of a closed month for every teacher
    with consumption in `institution` (None = every institution). Teachers without an email are skipped;
    those already queued or sent are skipped too unless `resend`; a FAILED one is retried. Returns the
    counts {queued, already_sent, without_email}.
    """
    period = closed_credit_month(month)
    result = {'queued': 0, 'already_sent': 0, 'without_email': 0}
    with transaction.atomic():
        for row in teacher_credit_summary(institution, period):
            if not row['customer__email']:
                result['without_email'] += 1
                continue
            statement, created = CreditStatement.objects.get_or_create(
                teacher_id=row['customer_id'], institution_id=row['institution_id'], month=month,
                defaults={'total': row['total'], 'sent_by': admin},
            )
            if not created:
                if not resend and statement.status != CreditStatement.Status.FAILED:
                    result['already_sent'] += 1
                    continue
                statement.total, statement.sent_by = row['total'], admin
                statement.status, statement.sent_at = CreditStatement.Status.QUEUED, None
                statement.save()
            transaction.on_commit(lambda pk=statement.pk: _enqueue_credit_statement(pk))
            result['queued'] += 1
    return result


def _enqueue_credit_statement(statement_id):
    """The rows are already committed, so an unreachable broker must not turn the request into a 500:
    the statement is marked FAILED and the administrator can send it again."""
    try:
        send_credit_statement_email.delay(statement_id)
    except (OperationalError, OSError):
        logger.exception('Could not enqueue the credit statement %s', statement_id)
        fail_credit_statement(statement_id)


def fail_credit_statement(statement_id):
    CreditStatement.objects.filter(pk=statement_id).update(status=CreditStatement.Status.FAILED)


def deliver_credit_statement(statement_id):
    """
    Emails one credit statement: the teacher's total of the month and each consumption (the same sales the
    report sent to Human Resources is built from). Marks it SENT. Delivery errors propagate so the Celery
    task can retry; amounts and email addresses are never logged.
    """
    statement = CreditStatement.objects.select_related('teacher', 'institution').get(pk=statement_id)
    if statement.status == CreditStatement.Status.SENT:
        return
    teacher = statement.teacher
    if not teacher.email:
        fail_credit_statement(statement_id)
        return
    start, end = closed_credit_month(statement.month)
    sales = list(
        teacher_credit_sales(statement.institution, (start, end)).filter(customer=teacher)
        .prefetch_related('items__menu_item', 'combos').order_by('created_at', 'id')
    )
    total = sum((sale.total_amount for sale in sales), Decimal('0.00'))
    consumptions = []
    for sale in sales:
        when = sale.created_at.astimezone(LOCAL_TIMEZONE)
        consumptions.append({
            'date': f'{when:%d/%m/%Y}', 'time': f'{when:%H:%M}',
            'description': TeacherCreditReportBuilder.sale_description(sale), 'amount': f'${sale.total_amount:,.2f}',
        })
    context = {
        'first_name': teacher.first_name,
        'month_label': date_format(start, r'F \d\e Y'),
        'institution': statement.institution.name,
        'total': f'${total:,.2f}',
        'consumptions': consumptions,
    }
    subject = gettext("Estado de cuenta a crédito de %(month_label)s - D'Elycattessen") % context
    lines = [
        gettext('Hola, %(first_name)s') % context, '',
        gettext('Este es el estado de cuenta de tus consumos a crédito de %(month_label)s en %(institution)s.') % context,
        gettext('Total a descontar: %(total)s') % context, '',
    ]
    lines += [f"{c['date']} {c['time']}  {c['description']}  {c['amount']}" for c in consumptions]
    lines += ['', gettext('Este valor se descontará de tu rol de pagos. Si tienes dudas, comunícate con el administrador.')]
    message = EmailMultiAlternatives(
        subject=subject, body='\n'.join(lines), from_email=settings.DEFAULT_FROM_EMAIL, to=[teacher.email],
    )
    message.attach_alternative(render_to_string('emails/credit_statement.html', context), 'text/html')
    message.send()
    statement.total, statement.status, statement.sent_at = total, CreditStatement.Status.SENT, local_now()
    statement.save(update_fields=['total', 'status', 'sent_at'])


def monthly_credit_total(customer):
    """What a teacher has accumulated on institutional credit this month."""
    total = Sale.objects.filter(
        customer=customer,
        payment_method=Sale.PaymentMethod.CREDIT,
        created_at__gte=month_start_local(),
    ).aggregate(total=Sum('total_amount'))['total']
    return total or Decimal('0.00')


class PreOrderCreator(AllergenValidatorMixin, ParentalControlValidatorMixin):
    """
    Creates a pre-order for a child of the authenticated parent: checks the
    delivery date, the published menu, the dishes, parental controls and
    allergens, then debits the wallet. Everything runs in one transaction.
    """

    def __init__(self, parent_profile):
        self.parent_profile = parent_profile

    @transaction.atomic
    def create(self, student_id, delivery_date, items_data):
        student = self._get_own_student(student_id)

        if delivery_date <= local_today():
            raise ValidationError(
                _("No se puede preordenar para hoy o fechas pasadas. La fecha de entrega debe ser posterior a hoy.")
            )

        daily_menu = self._get_published_menu(student, delivery_date)
        order_lines, total_amount = self._build_lines(daily_menu, items_data)

        self.validate_parental_controls(student, total_amount, target_date=delivery_date)
        self.validate_allergens(student, [dish for dish, _quantity in order_lines])

        pre_order = PreOrder.objects.create(
            student=student,
            daily_menu=daily_menu,
            total_amount=total_amount,
            status=PreOrder.Status.PENDING,
        )
        PreOrderItem.objects.bulk_create([
            PreOrderItem(pre_order=pre_order, dish=dish, quantity=quantity, price_at_purchase=dish.price)
            for dish, quantity in order_lines
        ])
        debit_wallet(student.wallet, total_amount, pre_order=pre_order)
        return pre_order

    def _get_own_student(self, student_id):
        try:
            student = StudentProfile.objects.active().select_related('institution', 'wallet', 'parent').get(id=student_id)
        except StudentProfile.DoesNotExist:
            raise ValidationError({'student_id': _('Estudiante no encontrado.')})
        if not self.parent_profile or student.parent_id != self.parent_profile.id:
            raise PermissionDenied(_('No tienes permiso sobre este estudiante.'))
        return student

    @staticmethod
    def _get_published_menu(student, delivery_date):
        try:
            return DailyMenu.objects.select_related('monthly_menu').get(
                date=delivery_date,
                monthly_menu__institution=student.institution,
                monthly_menu__status=MonthlyMenu.Status.PUBLISHED,
            )
        except DailyMenu.DoesNotExist:
            raise ValidationError(
                _("No hay un menú publicado para esa fecha en la institución del estudiante.")
            )

    @staticmethod
    def _build_lines(daily_menu, items_data):
        available_dishes = {dish.id: dish for dish in daily_menu.dishes.filter(is_active=True)}
        order_lines = []
        total_amount = Decimal('0.00')
        for item_data in items_data:
            dish = available_dishes.get(item_data['dish_id'])
            if dish is None:
                raise ValidationError(
                    _("El platillo solicitado no está disponible en el menú de esa fecha.")
                )
            order_lines.append((dish, item_data['quantity']))
            total_amount += dish.price * item_data['quantity']
        return order_lines, total_amount


def deliver_preorder(pre_order):
    """Marks a pending pre-order as delivered; only on its own delivery day."""
    if pre_order.status != PreOrder.Status.PENDING:
        raise ValidationError({'detail': _("Esta preorden no está pendiente de entrega.")})
    if pre_order.daily_menu.date != local_today():
        raise ValidationError({'detail': _("Esta preorden es para el %(date)s, no para hoy.") % {
            'date': pre_order.daily_menu.date.strftime('%d/%m/%Y'),
        }})
    pre_order.status = PreOrder.Status.DELIVERED
    pre_order.save(update_fields=['status'])
    return pre_order


def consume_qr_nonce(nonce):
    """Marks a QR nonce as used. False when it was already used (replay)."""
    return cache.add(f'used_qr_nonce_{nonce}', True, timeout=settings.QR_NONCE_TTL_SECONDS)


def build_identified_user_payload(user, method):
    """Standard summary of an identified person (face or QR) for the POS screen."""
    balance = '0.00'
    allergies = [a.name for a in user.allergies.all()]
    student_id = None
    institution = user.institution  # staff/teacher institution
    pending_orders = []

    if hasattr(user, 'student_profile'):
        student_profile = user.student_profile
        student_id = student_profile.id
        allergies = [a.name for a in student_profile.allergies.all()]
        # A student's institution lives on StudentProfile, not on the
        # CustomUser itself (that field is only populated for staff).
        institution = student_profile.institution
        if hasattr(student_profile, 'wallet'):
            balance = str(student_profile.wallet.balance)

        pending_preorders = PreOrder.objects.filter(
            student=student_profile,
            status=PreOrder.Status.PENDING,
            daily_menu__date=local_today(),
        ).select_related('daily_menu').prefetch_related('items__dish').order_by('created_at')

        pending_orders = [
            {
                'pre_order_id': pre_order.id,
                'total_amount': str(pre_order.total_amount),
                'created_at': pre_order.created_at.isoformat(),
                'delivery_date': str(pre_order.delivery_date),
                'items': [
                    {
                        'dish_name': item.dish.name,
                        'quantity': item.quantity,
                        'price_at_purchase': str(item.price_at_purchase),
                    }
                    for item in pre_order.items.all()
                ],
            }
            for pre_order in pending_preorders
        ]
    elif hasattr(user, 'wallet'):
        balance = str(user.wallet.balance)

    # Teachers do not use a wallet: the cashier sees what they owe on credit.
    credit_accumulated = None
    if user.role == CustomUser.Role.TEACHER:
        credit_accumulated = str(monthly_credit_total(user))

    return {
        'user_id': user.id,
        'student_id': student_id,
        'username': user.username,
        'full_name': f'{user.first_name} {user.last_name}'.strip() or user.username,
        'role': user.role,
        'role_display': user.get_role_display(),
        'institution': institution.name if institution else None,
        'balance': balance,
        'credit_accumulated': credit_accumulated,
        'allergies': sorted(set(allergies)),
        'pending_orders': pending_orders,
        'identification_method': method,
    }


CHANNEL_PREORDER = 'PREORDER'
# Sales channels shown on the dashboard, in display order: the three POS payment
# methods plus pre-orders (always paid from the wallet when they are created).
CHANNELS = (
    Sale.PaymentMethod.CASH.value,
    Sale.PaymentMethod.WALLET.value,
    Sale.PaymentMethod.CREDIT.value,
    CHANNEL_PREORDER,
)
GRANULARITIES = ('day', 'week', 'month')
_ZERO = Decimal('0.00')
_TRUNC_BY_GRANULARITY = {'day': TruncDate, 'week': TruncWeek, 'month': TruncMonth}
_LINE_TOTAL = ExpressionWrapper(
    F('quantity') * F('price_at_purchase'),
    output_field=DecimalField(max_digits=12, decimal_places=2),
)


def _money(value):
    return str(Decimal(value).quantize(_ZERO))


def _change_pct(current, previous):
    """Percentage change against the previous period; None when there is nothing to compare."""
    if not previous:
        return None
    return round(float((Decimal(current) - Decimal(previous)) / Decimal(previous) * 100), 1)


def _local_day_start(day):
    return datetime.combine(day, time.min, tzinfo=LOCAL_TIMEZONE)


def _bucket_start(day, granularity):
    if granularity == 'week':
        return day - timedelta(days=day.weekday())
    if granularity == 'month':
        return day.replace(day=1)
    return day


def _next_bucket_start(start, granularity):
    if granularity == 'week':
        return start + timedelta(days=7)
    if granularity == 'month':
        return (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start + timedelta(days=1)


class SalesAnalytics:
    """
    Read-only sales metrics for the admin dashboard over an inclusive range of
    local dates. `institution=None` consolidates every institution.

    Revenue is every Sale (POS: cash, wallet, credit) plus every pre-order that
    is not canceled, counted when it is created. Wallet consumption transactions
    are not added (they duplicate the sale) and recharges are not revenue.
    Days are cut in the business time zone, not in UTC.
    """

    def __init__(self, institution, date_from, date_to):
        self.institution = institution
        self.date_from = date_from
        self.date_to = date_to
        days = (date_to - date_from).days + 1
        self.previous_to = date_from - timedelta(days=1)
        self.previous_from = self.previous_to - timedelta(days=days - 1)

    # ---- querysets ------------------------------------------------------
    @staticmethod
    def _bounds(date_from, date_to):
        return _local_day_start(date_from), _local_day_start(date_to + timedelta(days=1))

    def _sales(self, date_from, date_to):
        start, end = self._bounds(date_from, date_to)
        sales = Sale.objects.filter(created_at__gte=start, created_at__lt=end)
        if self.institution is not None:
            sales = sales.filter(institution=self.institution)
        return sales

    def _preorders(self, date_from, date_to):
        start, end = self._bounds(date_from, date_to)
        preorders = PreOrder.objects.filter(created_at__gte=start, created_at__lt=end).exclude(
            status=PreOrder.Status.CANCELED,
        )
        if self.institution is not None:
            preorders = preorders.filter(student__institution=self.institution)
        return preorders

    # ---- summary --------------------------------------------------------
    def _channel_totals(self, date_from, date_to):
        totals = {channel: {'amount': _ZERO, 'count': 0} for channel in CHANNELS}
        rows = self._sales(date_from, date_to).order_by().values('payment_method').annotate(
            amount=Sum('total_amount'), count=Count('id'),
        )
        for row in rows:
            totals[row['payment_method']] = {'amount': row['amount'], 'count': row['count']}
        preorders = self._preorders(date_from, date_to).aggregate(amount=Sum('total_amount'), count=Count('id'))
        totals[CHANNEL_PREORDER] = {'amount': preorders['amount'] or _ZERO, 'count': preorders['count']}
        return totals

    def _window(self, date_from, date_to):
        channels = self._channel_totals(date_from, date_to)
        revenue = sum((data['amount'] for data in channels.values()), _ZERO)
        transactions = sum(data['count'] for data in channels.values())
        average_ticket = revenue / transactions if transactions else _ZERO
        return {
            'channels': channels,
            'revenue': revenue,
            'transactions': transactions,
            'average_ticket': average_ticket,
        }

    @staticmethod
    def _kpi(current, previous, fmt=lambda value: value):
        return {'current': fmt(current), 'previous': fmt(previous), 'change_pct': _change_pct(current, previous)}

    def summary(self):
        current = self._window(self.date_from, self.date_to)
        previous = self._window(self.previous_from, self.previous_to)
        revenue = current['revenue']
        channels = []
        for channel in CHANNELS:
            now, before = current['channels'][channel], previous['channels'][channel]
            channels.append({
                'channel': channel,
                'amount': _money(now['amount']),
                'count': now['count'],
                'share_pct': round(float(now['amount'] / revenue * 100), 1) if revenue else 0.0,
                'previous_amount': _money(before['amount']),
                'change_pct': _change_pct(now['amount'], before['amount']),
            })
        return {
            'period': {'date_from': self.date_from, 'date_to': self.date_to},
            'previous_period': {'date_from': self.previous_from, 'date_to': self.previous_to},
            'kpis': {
                'revenue': self._kpi(revenue, previous['revenue'], _money),
                'transactions': self._kpi(current['transactions'], previous['transactions']),
                'average_ticket': self._kpi(current['average_ticket'], previous['average_ticket'], _money),
            },
            'channels': channels,
        }

    # ---- time series ----------------------------------------------------
    def _bucket_rows(self, queryset, granularity, group_by):
        bucket = _TRUNC_BY_GRANULARITY[granularity]('created_at', tzinfo=LOCAL_TIMEZONE)
        return queryset.order_by().annotate(bucket=bucket).values('bucket', *group_by).annotate(
            amount=Sum('total_amount'), count=Count('id'),
        )

    @staticmethod
    def _as_local_date(value):
        return value.astimezone(LOCAL_TIMEZONE).date() if isinstance(value, datetime) else value

    def timeseries(self, granularity):
        """One entry per day/week/month of the range (empty ones included), with
        the amount per channel and the exact date range of the bucket so the
        dashboard can drill down by asking for that range."""
        buckets = {}
        start = _bucket_start(self.date_from, granularity)
        while start <= self.date_to:
            end = _next_bucket_start(start, granularity) - timedelta(days=1)
            buckets[start] = {
                'bucket_start': start,
                'date_from': max(start, self.date_from),
                'date_to': min(end, self.date_to),
                'revenue': _ZERO,
                'transactions': 0,
                'channels': {channel: _ZERO for channel in CHANNELS},
            }
            start = _next_bucket_start(start, granularity)

        sales = self._bucket_rows(self._sales(self.date_from, self.date_to), granularity, ('payment_method',))
        preorders = self._bucket_rows(self._preorders(self.date_from, self.date_to), granularity, ())
        for rows, channel_of in ((sales, lambda row: row['payment_method']), (preorders, lambda row: CHANNEL_PREORDER)):
            for row in rows:
                entry = buckets[self._as_local_date(row['bucket'])]
                entry['channels'][channel_of(row)] += row['amount']
                entry['revenue'] += row['amount']
                entry['transactions'] += row['count']

        return [
            {
                **entry,
                'revenue': _money(entry['revenue']),
                'channels': {channel: _money(amount) for channel, amount in entry['channels'].items()},
            }
            for entry in buckets.values()
        ]

    # ---- top products ---------------------------------------------------
    def _product_totals(self):
        """Units and revenue per product name, POS items and pre-order dishes
        together (they are different tables but the same dish to the admin)."""
        start, end = self._bounds(self.date_from, self.date_to)
        sale_items = SaleItem.objects.filter(sale__created_at__gte=start, sale__created_at__lt=end)
        preorder_items = PreOrderItem.objects.filter(
            pre_order__created_at__gte=start, pre_order__created_at__lt=end,
        ).exclude(pre_order__status=PreOrder.Status.CANCELED)
        if self.institution is not None:
            sale_items = sale_items.filter(sale__institution=self.institution)
            preorder_items = preorder_items.filter(pre_order__student__institution=self.institution)

        rows = list(
            sale_items.order_by().values(name=F('menu_item__name')).annotate(
                units=Sum('quantity'), revenue=Sum(_LINE_TOTAL),
            )
        ) + list(
            preorder_items.order_by().values(name=F('dish__name')).annotate(
                units=Sum('quantity'), revenue=Sum(_LINE_TOTAL),
            )
        )
        products = {}
        for row in rows:
            product = products.setdefault(
                row['name'].strip().casefold(), {'name': row['name'].strip(), 'units': 0, 'revenue': _ZERO},
            )
            product['units'] += row['units']
            product['revenue'] += row['revenue']
        return list(products.values())

    def top_products(self):
        return self._rankings(self._product_totals())

    # ---- combos -----------------------------------------------------------
    def top_combos(self):
        """Combos sold at the POS, each one as a single line at its own price
        (grouped by the name saved on the sale, so deleted combos still count),
        ranked like the products, plus the totals of the period."""
        start, end = self._bounds(self.date_from, self.date_to)
        lines = SaleCombo.objects.filter(sale__created_at__gte=start, sale__created_at__lt=end)
        if self.institution is not None:
            lines = lines.filter(sale__institution=self.institution)
        combos = list(
            lines.order_by().values('name').annotate(units=Sum('quantity'), revenue=Sum(_LINE_TOTAL))
        )
        return {
            **self._rankings(combos),
            'totals': {
                'units': sum(combo['units'] for combo in combos),
                'revenue': _money(sum((combo['revenue'] for combo in combos), _ZERO)),
            },
        }

    @staticmethod
    def _rankings(entries):
        """Top `ANALYTICS_TOP_PRODUCTS_LIMIT` of {name, units, revenue} rows by revenue and by units."""
        limit = settings.ANALYTICS_TOP_PRODUCTS_LIMIT

        def ranking(sort_key):
            ordered = sorted(entries, key=sort_key)[:limit]
            return [
                {'name': entry['name'], 'units': entry['units'], 'revenue': _money(entry['revenue'])}
                for entry in ordered
            ]

        return {
            'by_revenue': ranking(lambda entry: (-entry['revenue'], entry['name'])),
            'by_units': ranking(lambda entry: (-entry['units'], entry['name'])),
        }
