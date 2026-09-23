"""Google Sheets projection: idempotent row writes and the owner "Обзор" dashboard.

PostgreSQL owns event identity and idempotency; Sheets is a russian-language view for the
owner. Every worker-written row carries a technical ``NotiMate Event ID`` so a retried
event never appends the same row twice (see ``append_rows_once``).
"""

from __future__ import annotations

import datetime
import pytz

import app
from logging_utils import get_logger
from notimate.timeutil import bangkok_date, days_until

logger = get_logger()

EVENT_ID_HEADER = 'NotiMate Event ID'


def get_or_create_sheet(sh, name, headers):
    try:
        ws = sh.worksheet(name)
    except:
        ws = sh.add_worksheet(title=name, rows=1000, cols=len(headers) + 1)
        ws.append_row(headers + [EVENT_ID_HEADER])
    ensure_event_id_column(ws)
    return ws


def ensure_event_id_column(ws):
    """Return the one-based technical event-ID column, adding it if necessary.

    The ID is deliberately stored in the spreadsheet: a worker can be killed after
    Google accepted an append but before it acknowledges the event in Postgres.
    On retry, this durable marker makes the projection safe to repeat.
    """
    headers = ws.row_values(1)
    if EVENT_ID_HEADER not in headers:
        column = len(headers) + 1
        # Existing client sheets may have an exact-width grid.  Extending the
        # grid before writing the technical column keeps idempotency compatible
        # with those sheets instead of failing with Google API 400 grid limits.
        current_columns = getattr(ws, 'col_count', None)
        if current_columns is not None and current_columns < column:
            ws.add_cols(column - current_columns)
        ws.update_cell(1, column, EVENT_ID_HEADER)
        return column
    return headers.index(EVENT_ID_HEADER) + 1


def append_rows_once(ws, rows, event_id, effect):
    """Append the rows not yet projected for one LINE event and verify the write."""
    if not event_id:
        raise ValueError('Cannot write to Google Sheets without webhookEventId')
    if not rows:
        return 0
    event_column = ensure_event_id_column(ws)
    keys = [f'{event_id}:{effect}:{index}' for index in range(len(rows))]
    existing_keys = set(ws.col_values(event_column)[1:])
    missing = []
    for row, key in zip(rows, keys):
        if key in existing_keys:
            continue
        padded = list(row[:event_column - 1])
        padded.extend([''] * (event_column - 1 - len(padded)))
        padded.append(key)
        missing.append(padded)
    if not missing:
        return 0
    ws.append_rows(missing, value_input_option='USER_ENTERED')
    written_keys = set(ws.col_values(event_column)[1:])
    expected = {key for key in keys if key not in existing_keys}
    if not expected.issubset(written_keys):
        raise RuntimeError('Google Sheets did not confirm all appended event rows')
    return len(missing)

def get_last_date(ws):
    try:
        col = ws.col_values(1)
        for val in reversed(col):
            if val and val != 'Дата':
                return val
    except:
        pass
    return None

def save_остатки(sheet_id, items, date_str, event_id):
    if not app.gc:
        raise RuntimeError('Google Sheets is not ready')
    sh = app.gc.open_by_key(sheet_id)
    headers = ['Дата', 'Категория', 'Продукт', 'Холодильник', 'Морозилка', 'Примечание']
    ws = get_or_create_sheet(sh, 'Остатки', headers)
    last_date = get_last_date(ws)
    last_category = None
    rows = []
    for i, item in enumerate(items):
        date_cell = date_str if (i == 0 and last_date != date_str) else ''
        category = item.get('category', '')
        category_cell = category if category != last_category else ''
        if category:
            last_category = category
        rows.append([date_cell, category_cell, item.get('product',''), item.get('fridge',''), item.get('freezer',''), item.get('note','')])
    return append_rows_once(ws, rows, event_id, 'stock')

def save_закупки(sheet_id, items, date_str, event_id):
    if not app.gc:
        raise RuntimeError('Google Sheets is not ready')
    sh = app.gc.open_by_key(sheet_id)
    headers = ['Дата', 'Продукт', 'Количество']
    ws = get_or_create_sheet(sh, 'Закупки', headers)
    last_date = get_last_date(ws)
    rows = []
    for i, item in enumerate(items):
        date_cell = date_str if (i == 0 and last_date != date_str) else ''
        rows.append([date_cell, item.get('product',''), item.get('quantity','')])
    return append_rows_once(ws, rows, event_id, 'purchase')

def save_расходы(sheet_id, items, date_str, supplier, note='', event_id=None, effect='expense'):
    if not app.gc:
        raise RuntimeError('Google Sheets is not ready')
    sh = app.gc.open_by_key(sheet_id)
    headers = ['Дата', 'Тип', 'Поставщик/Магазин', 'Позиция', 'Сумма (THB)', 'Примечание']
    ws = get_or_create_sheet(sh, 'Расходы', headers)
    rows = []
    for item in items:
        clean_amount = str(item.get('amount','') or '').replace('฿','').replace('B','').replace(',','').strip()
        rows.append([date_str, item.get('type','Закупка'), item.get('supplier', supplier), item.get('description',''), clean_amount, item.get('note', note)])
    return append_rows_once(ws, rows, event_id, effect)

def save_выручка(sheet_id, data, date_str, note='', event_id=None):
    if not app.gc:
        raise RuntimeError('Google Sheets is not ready')
    sh = app.gc.open_by_key(sheet_id)
    headers = ['Дата', 'Смена', 'Gross Sales', 'Наличные', 'Карта', 'QR', 'Примечание']
    ws = get_or_create_sheet(sh, 'Выручка', headers)
    return append_rows_once(ws, [[date_str, data.get('shift',''), data.get('gross_sales',''), data.get('cash',''), data.get('card',''), data.get('qr',''), note]], event_id, 'shift')

def save_проблемы(sheet_id, text, result, date_str, event_id):
    if not app.gc:
        raise RuntimeError('Google Sheets is not ready')
    sh = app.gc.open_by_key(sheet_id)
    headers = ['Дата', 'Сообщение', 'Перевод и совет']
    ws = get_or_create_sheet(sh, 'Проблемы', headers)
    return append_rows_once(ws, [[date_str, text, result]], event_id, 'problem')


def save_одиночный_остаток(sheet_id, product, amount, date_str, event_id):
    sh = app.gc.open_by_key(sheet_id)
    ws = get_or_create_sheet(sh, 'Остатки', ['Дата','Категория','Продукт','Холодильник','Морозилка','Примечание'])
    return append_rows_once(ws, [[date_str, '', product, amount, '', '']], event_id, 'single-stock')


def save_зарплаты(sheet_id, items, date_str, event_id):
    sh = app.gc.open_by_key(sheet_id)
    ws = get_or_create_sheet(sh, 'Зарплаты', ['Дата','Получатель','Сумма (THB)','Примечание'])
    rows = [[date_str, item.get('recipient',''), item.get('amount',''), item.get('note','')] for item in items]
    return append_rows_once(ws, rows, event_id, 'salary')


def save_напоминание(sheet_id, data, date_str, event_id):
    sh = app.gc.open_by_key(sheet_id)
    ws = get_or_create_sheet(sh, 'Напоминания', ['Название', 'Дата окончания', 'Дата добавления', 'Примечание'])
    row = [data.get('title',''), data.get('expiry_date',''), date_str, data.get('note','')]
    return append_rows_once(ws, [row], event_id, 'reminder')

# ── Дрейф цен ────────────────────────────────────────────────────
def check_price_drift(sheet_id, items, supplier, client_cfg, event_id):
    """Проверяет дрейф цен и уведомляет если цена выросла >10%"""
    if not app.gc:
        return
    try:
        sh = app.gc.open_by_key(sheet_id)
        headers = ['Дата', 'Поставщик', 'Позиция', 'Цена (THB)']
        ws = get_or_create_sheet(sh, 'Цены', headers)
        rows = ws.get_all_records()
        alerts = []
        price_rows = []
        date_today = bangkok_date()
        for item in items:
            name = item.get('description', '').strip()
            try:
                price = float(str(item.get('unit_price', item.get('amount', 0)) or 0).replace('฿','').replace(',','').strip() or 0)
            except:
                price = 0
            if not name or price <= 0:
                continue
            # Ищем последнюю цену этой позиции
            prev_price = None
            for row in reversed(rows):
                if row.get('Позиция','').lower() == name.lower():
                    try:
                        prev_price = float(str(row.get('Цена (THB)', 0) or 0).replace('฿','').replace(',','').strip() or 0)
                    except:
                        prev_price = None
                    break
            price_rows.append([date_today, supplier, name, price])
            # Проверяем дрейф
            if prev_price and prev_price > 0 and price > 0:
                drift = (price - prev_price) / prev_price * 100
                if drift >= 10:
                    alerts.append(f"- {name}: {prev_price:.0f} → {price:.0f} THB (+{drift:.0f}%)")
        created = append_rows_once(ws, price_rows, event_id, 'price')
        if alerts and created:
            msg = f"⚠️ ДРЕЙФ ЦЕН от {supplier}:\n"
            msg += "\n".join(alerts)
            msg += "\n\n💡 Проверьте накладную — поставщик поднял цены."
            app.notify_owner(client_cfg, msg)
    except Exception as exc:
        logger.error('price_drift_check_failed', extra={'event_id': event_id, 'error_type': type(exc).__name__})


def _money(value):
    try:
        return float(str(value or 0).replace('฿', '').replace(',', '').replace('B', '').strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _rows_for_date(rows, date_prefix, amount_field):
    return sum(_money(row.get(amount_field)) for row in rows if str(row.get('Дата', '')).startswith(date_prefix))


def _dashboard_label(value, limit=22):
    """Keep chart labels scannable without changing source worksheet data."""
    label = ' '.join(str(value or '').split()) or 'Без поставщика'
    return label if len(label) <= limit else f'{label[:limit - 1].rstrip()}…'


def upcoming_reminders(sh, now, days_limit=14, limit=5):
    try:
        rows = sh.worksheet('Напоминания').get_all_records()
    except Exception:
        return []
    reminders = []
    for row in rows:
        expiry = str(row.get('Дата окончания', '')).strip()
        title = str(row.get('Название', '')).strip()
        if not expiry or not title:
            continue
        try:
            left = days_until(expiry, now=now)
        except Exception:
            continue
        if 0 <= left <= days_limit:
            reminders.append((left, title, expiry))
    return sorted(reminders)[:limit]


def refresh_overview(client_cfg):
    """Refresh the owner dashboard after a confirmed projection."""
    if not app.gc:
        raise RuntimeError('Google Sheets is not ready')
    tz = pytz.timezone('Asia/Bangkok')
    now = datetime.datetime.now(tz)
    sh = app.gc.open_by_key(client_cfg['sheet_id'])

    try:
        revenue_rows = sh.worksheet('Выручка').get_all_records()
    except Exception:
        revenue_rows = []
    try:
        expense_rows = sh.worksheet('Расходы').get_all_records()
    except Exception:
        expense_rows = []
    # «Сегодня» — календарная дата Asia/Bangkok, а не последняя запись в таблице:
    # день или месяц без записей показывает нули, а не цифры старого периода.
    dashboard_date = now.strftime('%Y-%m-%d')
    dashboard_day = datetime.datetime.strptime(dashboard_date, '%Y-%m-%d').replace(tzinfo=tz)
    month = dashboard_date[:7]
    revenue_today = _rows_for_date(revenue_rows, dashboard_date, 'Gross Sales')
    expenses_today = _rows_for_date(expense_rows, dashboard_date, 'Сумма (THB)')
    revenue_month = _rows_for_date(revenue_rows, month, 'Gross Sales')
    expenses_month = _rows_for_date(expense_rows, month, 'Сумма (THB)')
    daily = {}
    for offset in range(13, -1, -1):
        day = (dashboard_day - datetime.timedelta(days=offset)).strftime('%Y-%m-%d')
        daily[day] = [
            _rows_for_date(revenue_rows, day, 'Gross Sales'),
            _rows_for_date(expense_rows, day, 'Сумма (THB)'),
        ]
    suppliers = {}
    for row in expense_rows:
        if not str(row.get('Дата', '')).startswith(month):
            continue
        supplier = str(row.get('Поставщик/Магазин', '')).strip() or 'Без поставщика'
        suppliers[supplier] = suppliers.get(supplier, 0.0) + _money(row.get('Сумма (THB)'))
    top_suppliers = sorted(suppliers.items(), key=lambda item: item[1], reverse=True)[:6] or [('Нет данных', 0)]

    critical = []
    try:
        stock_rows = sh.worksheet('Остатки').get_all_records()
        current_date = ''
        dated_rows = []
        for row in stock_rows:
            if str(row.get('Дата', '')).strip():
                current_date = str(row.get('Дата', '')).strip()
            if row.get('Продукт'):
                dated_rows.append((current_date, row))
        latest_date = max((date for date, _ in dated_rows if date), default='')
        critical = [
            row for date, row in dated_rows
            if date == latest_date and row.get('Примечание') in ('Out of stock', 'Low stock', 'Exp today')
        ][:10]
    except Exception:
        pass
    reminders = upcoming_reminders(sh, now, days_limit=14, limit=10)

    values = [['' for _ in range(18)] for _ in range(37)]

    def put(row, column, value):
        values[row - 1][column - 1] = value

    display_date = dashboard_day.strftime('%d.%m.%Y')
    put(1, 1, 'NotiMate')
    put(3, 1, 'Ваш бизнес под контролем')
    put(1, 4, 'Обзор владельца')
    put(3, 4, 'Продажи · Расходы · Остатки · Сроки годности')
    put(1, 11, 'Период')
    put(2, 11, display_date)
    put(1, 14, 'Обновлено (Bangkok)')
    put(2, 14, now.strftime('%d.%m.%Y %H:%M'))
    put(3, 14, f'Последний день данных: {display_date}')
    put(1, 17, 'Данные из Google Sheets')
    put(3, 17, 'Обновляется автоматически')

    cards = [
        (1, f'Выручка · {display_date}', revenue_today, 'Выручка за месяц', revenue_month),
        (5, f'Расходы · {display_date}', expenses_today, 'Расходы за месяц', expenses_month),
        (9, 'Результат дня', revenue_today - expenses_today, 'Результат за месяц', revenue_month - expenses_month),
        (13, 'Критичных позиций', len(critical), 'Сроков ≤ 14 дней', len(reminders)),
    ]
    for column, day_label, day_value, month_label, month_value in cards:
        put(5, column, day_label)
        put(6, column, day_value)
        put(8, column, month_label)
        put(9, column, month_value)

    put(12, 15, 'Товары по срокам годности')
    put(14, 15, f'{len(reminders)}\nтоваров ≤ 14 дней')
    if reminders:
        left, title, expiry = reminders[0]
        put(18, 15, f'Ближайший: {title} · {left} дн.')
    put(19, 15, 'Нет сроков в ближайшие 14 дней' if not reminders else 'Проверьте ближайшие сроки')
    put(21, 15, 'Отлично! Можно не беспокоиться.' if not reminders else 'Откройте вкладку «Напоминания».')
    put(26, 1, 'Критичные остатки')
    put(27, 1, 'Товар')
    put(27, 5, 'Статус')
    put(27, 7, 'Холодильник')
    put(27, 9, 'Морозилка')
    put(26, 10, 'Финансовый итог за месяц')
    put(28, 10, 'Выручка')
    put(29, 10, 'Расходы')
    put(30, 10, 'Результат')
    put(28, 14, revenue_month)
    put(29, 14, expenses_month)
    put(30, 14, revenue_month - expenses_month)
    put(32, 10, 'Убыток за месяц' if revenue_month < expenses_month else 'Результат за месяц')
    put(33, 10, f"Расходы превышают выручку на {abs(revenue_month - expenses_month):,.0f} THB" if revenue_month < expenses_month else 'Выручка превышает расходы.')
    put(26, 15, 'Быстрые действия')
    put(28, 15, 'Добавить покупку — напишите в LINE')
    put(30, 15, 'Проверить остатки')
    put(32, 15, 'Открыть отчёты')
    put(34, 15, 'Настройки и напоминания')
    put(37, 1, 'NotiMate  ·  Данные из Google Sheets  ·  Обновляется автоматически')
    if critical:
        critical_rows = [[row.get('Продукт', ''), row.get('Примечание', ''), row.get('Холодильник', ''), row.get('Морозилка', '')] for row in critical]
    else:
        critical_rows = [['Нет критичных остатков', '', '', '']]
    if reminders:
        reminder_rows = [[title, expiry, left] for left, title, expiry in reminders]
    else:
        reminder_rows = [['Нет сроков в ближайшие 14 дней', '', '']]
    for index, row in enumerate(critical_rows[:7], start=28):
        put(index, 1, row[0])
        put(index, 5, row[1])
        put(index, 7, row[2])
        put(index, 9, row[3])
    trend_rows = [['Дата', 'Выручка (THB)', 'Расходы (THB)']] + [[day, revenue, expense] for day, (revenue, expense) in daily.items()]
    supplier_rows = [['Поставщик', 'Расходы (THB)']] + [[_dashboard_label(supplier), amount] for supplier, amount in top_suppliers]

    try:
        ws = sh.worksheet('Обзор')
    except Exception:
        ws = sh.add_worksheet(title='Обзор', rows=100, cols=12)
    if getattr(ws, 'col_count', 22) < 22:
        ws.add_cols(22 - ws.col_count)
    if hasattr(ws, 'unmerge_cells'):
        try:
            ws.unmerge_cells('A1:R40')
        except Exception:
            pass
    ws.batch_clear(['A1:V45'])
    ws.update(values=values, range_name='A1:R37', value_input_option='USER_ENTERED')
    ws.update(values=trend_rows, range_name=f'T1:V{len(trend_rows)}', value_input_option='USER_ENTERED')
    ws.update(values=supplier_rows, range_name=f'T20:U{19 + len(supplier_rows)}', value_input_option='USER_ENTERED')
    dark_green = {'red': 0.13, 'green': 0.31, 'blue': 0.24}
    light_green = {'red': 0.94, 'green': 0.98, 'blue': 0.95}
    pale_green = {'red': 0.9, 'green': 0.96, 'blue': 0.92}
    pale_red = {'red': 0.99, 'green': 0.91, 'blue': 0.91}
    pale_amber = {'red': 1.0, 'green': 0.96, 'blue': 0.86}
    pale_blue = {'red': 0.91, 'green': 0.95, 'blue': 1.0}
    merged_ranges = [
        'A1:C2', 'D1:I2', 'K1:M2', 'N1:P2', 'Q1:R2',
        'A5:D5', 'A6:D7', 'A8:D8', 'A9:D10',
        'E5:H5', 'E6:H7', 'E8:H8', 'E9:H10',
        'I5:L5', 'I6:L7', 'I8:L8', 'I9:L10',
        'M5:R5', 'M6:R7', 'M8:R8', 'M9:R10',
        'O12:R12', 'O14:R17', 'O18:R18', 'O19:R20', 'O21:R22',
        'A26:I26', 'J26:N26', 'O26:R26', 'J32:N32', 'J33:N34',
        'O28:R28', 'O30:R30', 'O32:R32', 'O34:R34', 'A37:R37',
    ]
    if hasattr(ws, 'merge_cells'):
        for cell_range in merged_ranges:
            try:
                ws.merge_cells(cell_range)
            except Exception:
                pass
    format_requests = []

    def format_sheet(cell_range, style):
        format_requests.append({'range': cell_range, 'format': style})

    format_sheet('A1:R3', {'backgroundColor': light_green, 'verticalAlignment': 'MIDDLE'})
    format_sheet('A1:C2', {'textFormat': {'bold': True, 'fontSize': 16, 'foregroundColor': dark_green}, 'horizontalAlignment': 'CENTER'})
    format_sheet('A3:C3', {'textFormat': {'italic': True, 'fontSize': 9, 'foregroundColor': {'red': 0.2, 'green': 0.45, 'blue': 0.31}}, 'horizontalAlignment': 'CENTER'})
    format_sheet('D1:I2', {'textFormat': {'bold': True, 'fontSize': 16, 'foregroundColor': {'red': 0.05, 'green': 0.12, 'blue': 0.23}}, 'verticalAlignment': 'BOTTOM'})
    format_sheet('D3:I3', {'textFormat': {'fontSize': 10, 'foregroundColor': {'red': 0.33, 'green': 0.39, 'blue': 0.46}}})
    format_sheet('K1:M2', {'backgroundColor': {'red': 1, 'green': 1, 'blue': 1}, 'textFormat': {'bold': True, 'fontSize': 10}, 'horizontalAlignment': 'CENTER', 'verticalAlignment': 'MIDDLE'})
    format_sheet('N1:P3', {'textFormat': {'fontSize': 9, 'foregroundColor': {'red': 0.33, 'green': 0.39, 'blue': 0.46}}, 'verticalAlignment': 'MIDDLE'})
    format_sheet('Q1:R3', {'textFormat': {'italic': True, 'fontSize': 9, 'foregroundColor': {'red': 0.2, 'green': 0.45, 'blue': 0.31}}, 'horizontalAlignment': 'CENTER', 'verticalAlignment': 'MIDDLE'})
    result_color = {'red': 0.88, 'green': 0.94, 'blue': 0.89} if revenue_today >= expenses_today else {'red': 0.98, 'green': 0.89, 'blue': 0.89}
    cards = [
        ('A5:D10', pale_green), ('E5:H10', pale_red), ('I5:L10', result_color), ('M5:R10', pale_amber),
    ]
    for cell_range, color in cards:
        format_sheet(cell_range, {'backgroundColor': color, 'verticalAlignment': 'MIDDLE'})
    format_sheet('A5:R5', {'textFormat': {'bold': True, 'fontSize': 10, 'foregroundColor': {'red': 0.12, 'green': 0.18, 'blue': 0.28}}})
    format_sheet('A6:L7', {'textFormat': {'bold': True, 'fontSize': 18}, 'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0 "THB"'}})
    format_sheet('M6:R7', {'textFormat': {'bold': True, 'fontSize': 18}, 'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0'}})
    format_sheet('A8:R8', {'textFormat': {'bold': True, 'fontSize': 10, 'foregroundColor': {'red': 0.2, 'green': 0.27, 'blue': 0.34}}})
    format_sheet('A9:L10', {'textFormat': {'bold': True, 'fontSize': 16}, 'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0 "THB"'}})
    format_sheet('M9:R10', {'textFormat': {'bold': True, 'fontSize': 16}, 'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0'}})
    format_sheet('O12:R22', {'backgroundColor': pale_blue, 'verticalAlignment': 'MIDDLE'})
    format_sheet('O12:R12', {'textFormat': {'bold': True, 'fontSize': 11}})
    format_sheet('O14:R17', {'textFormat': {'bold': True, 'fontSize': 18, 'foregroundColor': {'red': 0.05, 'green': 0.12, 'blue': 0.23}}, 'horizontalAlignment': 'CENTER', 'wrapStrategy': 'WRAP'})
    format_sheet('O18:R18', {'textFormat': {'fontSize': 9, 'foregroundColor': {'red': 0.33, 'green': 0.39, 'blue': 0.46}}})
    format_sheet('O19:R20', {'backgroundColor': pale_green, 'textFormat': {'bold': True, 'fontSize': 10, 'foregroundColor': {'red': 0.1, 'green': 0.42, 'blue': 0.22}}, 'verticalAlignment': 'MIDDLE'})
    format_sheet('O21:R22', {'backgroundColor': pale_green, 'textFormat': {'fontSize': 9, 'foregroundColor': {'red': 0.1, 'green': 0.42, 'blue': 0.22}}})
    format_sheet('A26:I26', {'backgroundColor': pale_red, 'textFormat': {'bold': True, 'fontSize': 12}, 'verticalAlignment': 'MIDDLE'})
    format_sheet('A27:I27', {'backgroundColor': {'red': 0.96, 'green': 0.97, 'blue': 0.98}, 'textFormat': {'bold': True, 'fontSize': 9}, 'horizontalAlignment': 'CENTER'})
    format_sheet('A28:I34', {'verticalAlignment': 'MIDDLE'})
    format_sheet('E28:F34', {'backgroundColor': pale_amber, 'horizontalAlignment': 'CENTER'})
    format_sheet('J26:N26', {'backgroundColor': pale_blue, 'textFormat': {'bold': True, 'fontSize': 12}, 'verticalAlignment': 'MIDDLE'})
    format_sheet('J28:N30', {'verticalAlignment': 'MIDDLE'})
    format_sheet('N28:N28', {'textFormat': {'bold': True, 'foregroundColor': {'red': 0.1, 'green': 0.45, 'blue': 0.22}}, 'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0 "THB"'}})
    format_sheet('N29:N30', {'textFormat': {'bold': True, 'foregroundColor': {'red': 0.8, 'green': 0.15, 'blue': 0.15}}, 'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0 "THB"'}})
    format_sheet('J32:N34', {'backgroundColor': pale_red, 'verticalAlignment': 'MIDDLE'})
    format_sheet('J32:N32', {'textFormat': {'bold': True, 'fontSize': 11, 'foregroundColor': {'red': 0.75, 'green': 0.14, 'blue': 0.14}}})
    format_sheet('J33:N34', {'textFormat': {'fontSize': 9, 'foregroundColor': {'red': 0.65, 'green': 0.2, 'blue': 0.2}}})
    format_sheet('O26:R26', {'backgroundColor': pale_amber, 'textFormat': {'bold': True, 'fontSize': 12}, 'verticalAlignment': 'MIDDLE'})
    format_sheet('O28:R34', {'backgroundColor': {'red': 1, 'green': 1, 'blue': 1}, 'textFormat': {'fontSize': 10}, 'verticalAlignment': 'MIDDLE'})
    format_sheet('A37:R37', {'backgroundColor': light_green, 'textFormat': {'fontSize': 9, 'foregroundColor': {'red': 0.25, 'green': 0.4, 'blue': 0.32}}, 'verticalAlignment': 'MIDDLE'})
    format_sheet('T1:V45', {'textFormat': {'foregroundColor': {'red': 1, 'green': 1, 'blue': 1}}})
    if hasattr(ws, 'batch_format'):
        ws.batch_format(format_requests)
    else:
        for request in format_requests:
            ws.format(request['range'], request['format'])
    return {'critical': len(critical), 'reminders': len(reminders)}


def refresh_overview_safely(client_cfg):
    """Keep dashboard refresh useful but never let it block a confirmed operation."""
    try:
        return app.refresh_overview(client_cfg)
    except Exception as exc:
        logger.warning('overview_refresh_failed', extra={'error_type': type(exc).__name__})
        return None
