"""Google Sheets → PostgreSQL synchronisation (twice a day + on demand).

The owner sees and edits the Google Sheet; the web dashboard reads PostgreSQL. Without this,
a number corrected by hand in the sheet never reaches the dashboard. The sync makes the sheet
the source of truth for the rows it holds:

* a sheet row that carries the technical «NotiMate Event ID» reuses exactly that key (so it
  never duplicates what the bot's dual-write already stored) and **updates** the ledger row if
  the owner edited it;
* a hand-typed row without an ID gets a stable content key ``sync:<tab>:<hash>:<n>`` — editing
  such a row changes its hash, so the old ledger row disappears and the new one appears;
* a ledger row whose sheet row was deleted is rejected (operations) / removed (mirror tables).

Safety: a tab that can't be read is skipped entirely (no deletions on a bad read), and if a
sync would remove more than a quarter of the rows in its window it removes nothing and reports
it instead — a broken read must never wipe the ledger. Everything runs in one transaction on a
single connection (a full JSC history syncs in seconds, not minutes).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal
from typing import Any

EVENT_ID_HEADER = 'NotiMate Event ID'
TABS = ('Выручка', 'Расходы', 'Зарплаты', 'Закупки', 'Остатки', 'Проблемы', 'Напоминания')
TAB_TABLE = {
    'Выручка': 'operations', 'Расходы': 'operations', 'Зарплаты': 'operations', 'Закупки': 'operations',
    'Остатки': 'stock_signals', 'Проблемы': 'issues', 'Напоминания': 'reminders',
}
WINDOW_DAYS = 400
MAX_STALE_SHARE = 0.25


def _money(value) -> float:
    """Same tolerance as the report code (``sheets._money``), without importing the app."""
    try:
        cleaned = str(value or 0)
        for symbol in ('฿', '₸', '₽', '$', '€', ',', 'B', ' ', '\u00a0'):
            cleaned = cleaned.replace(symbol, '')
        return float(cleaned.strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_date(value) -> str | None:
    try:
        return dt.date.fromisoformat(str(value).strip()[:10]).isoformat()
    except ValueError:
        return None


def _amount_column(row: dict) -> float | None:
    for name, value in row.items():
        if str(name).startswith('Сумма (') and str(value).strip() != '':
            return _money(value)
    return None


def _fingerprint(tab: str, day: str | None, row: dict) -> str:
    parts = [tab, day or ''] + [f'{k}={str(v).strip()}' for k, v in sorted(row.items()) if k != EVENT_ID_HEADER and str(v).strip() != '']
    return hashlib.sha1('|'.join(parts).encode('utf-8')).hexdigest()[:16]


def plan_tab(tab: str, rows: list[dict], currency: str) -> list[dict[str, Any]]:
    """Translate one tab's rows into ledger rows: ``{'table', 'key', 'values', 'event_keyed'}``."""
    out: list[dict[str, Any]] = []
    last_day = None
    seen: dict[str, int] = {}
    for row in rows:
        day = parse_date(row.get('Дата') or row.get('Дата добавления'))
        if tab in ('Закупки', 'Остатки'):
            # These tabs write the date on the first row of a group only.
            day = day or last_day
            last_day = day
        event_key = str(row.get(EVENT_ID_HEADER) or '').strip()
        base = _fingerprint(tab, day, row)
        n = seen.get(base, 0)
        seen[base] = n + 1
        key = event_key or f'sync:{tab}:{base}:{n}'
        values: dict[str, Any] | None = None
        if tab == 'Выручка' and day:
            details = {k: row.get(h) for k, h in (('cash', 'Наличные'), ('card', 'Карта'), ('qr', 'QR')) if row.get(h) not in (None, '')}
            values = {'operation_type': 'revenue', 'occurred_on': day, 'amount': _money(row.get('Gross Sales')), 'counterparty': None,
                      'description': f"Смена {row.get('Смена', '')}", 'details': details}
        elif tab == 'Расходы' and day:
            values = {'operation_type': 'expense', 'occurred_on': day, 'amount': _amount_column(row), 'counterparty': row.get('Поставщик/Магазин') or None,
                      'description': row.get('Позиция') or '', 'details': {}}
        elif tab == 'Зарплаты' and day:
            values = {'operation_type': 'salary', 'occurred_on': day, 'amount': _amount_column(row), 'counterparty': row.get('Получатель') or None,
                      'description': row.get('Примечание') or '', 'details': {}}
        elif tab == 'Закупки' and day:
            values = {'operation_type': 'purchase', 'occurred_on': day, 'amount': None, 'counterparty': None,
                      'description': row.get('Продукт') or '', 'details': {'quantity': row.get('Количество', '')}}
        elif tab == 'Остатки' and day and row.get('Продукт'):
            values = {'occurred_on': day, 'category': row.get('Категория') or '', 'product': row['Продукт'], 'fridge': str(row.get('Холодильник', '')),
                      'freezer': str(row.get('Морозилка', '')), 'note': row.get('Примечание') or ''}
        elif tab == 'Проблемы' and day:
            values = {'occurred_on': day, 'message': str(row.get('Сообщение', ''))[:2000], 'advice': str(row.get('Перевод и совет', ''))[:2000]}
        elif tab == 'Напоминания' and row.get('Название'):
            values = {'title': row['Название'], 'expiry_date': parse_date(row.get('Дата окончания')),
                      'added_on': parse_date(row.get('Дата добавления')) or dt.date.today().isoformat(), 'note': row.get('Примечание') or ''}
        if values is not None:
            out.append({'table': TAB_TABLE[tab], 'key': key, 'values': values, 'event_keyed': bool(event_key)})
    return out


# ── reconcile ───────────────────────────────────────────────────────────────────────────

COMPARE = {
    'operations': ('operation_type', 'occurred_on', 'amount', 'counterparty', 'description'),
    'stock_signals': ('occurred_on', 'category', 'product', 'fridge', 'freezer', 'note'),
    'issues': ('occurred_on', 'message', 'advice'),
    'reminders': ('title', 'expiry_date', 'added_on', 'note'),
}
DATE_COLUMN = {'operations': 'occurred_on', 'stock_signals': 'occurred_on', 'issues': 'occurred_on', 'reminders': 'created_at::date'}


def _norm(value: Any) -> Any:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()[:10]
    if value is None or value == '':
        return None
    if isinstance(value, (int, float, Decimal)):
        return round(float(value), 2)
    return value


def _same(table: str, existing: dict, values: dict) -> bool:
    return all(_norm(existing[col]) == _norm(values[col]) for col in COMPARE[table])


def _insert(conn, table: str, tenant_id: str, key: str, values: dict, currency: str) -> None:
    if table == 'operations':
        conn.execute(
            """INSERT INTO operations (tenant_id, event_key, operation_type, occurred_on, amount, currency, counterparty, description, details)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT (tenant_id, event_key) DO NOTHING""",
            (tenant_id, key, values['operation_type'], values['occurred_on'], values['amount'], currency, values['counterparty'], values['description'], json.dumps(values['details'], ensure_ascii=False)))
    elif table == 'stock_signals':
        conn.execute(
            """INSERT INTO stock_signals (tenant_id, event_key, occurred_on, category, product, fridge, freezer, note)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id, event_key) DO NOTHING""",
            (tenant_id, key, values['occurred_on'], values['category'], values['product'], values['fridge'], values['freezer'], values['note']))
    elif table == 'issues':
        conn.execute(
            'INSERT INTO issues (tenant_id, event_key, occurred_on, message, advice) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (tenant_id, event_key) DO NOTHING',
            (tenant_id, key, values['occurred_on'], values['message'], values['advice']))
    else:
        conn.execute(
            'INSERT INTO reminders (tenant_id, event_key, title, expiry_date, added_on, note) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id, event_key) DO NOTHING',
            (tenant_id, key, values['title'], values['expiry_date'], values['added_on'], values['note']))


def _update(conn, table: str, tenant_id: str, key: str, values: dict) -> None:
    cols = COMPARE[table]
    if table == 'operations':
        cols = tuple(c for c in cols if c != 'operation_type')  # never re-type a row
    sets = ', '.join(f'{c} = %s' for c in cols) + (", status = 'confirmed'" if table == 'operations' else '')
    conn.execute(f'UPDATE {table} SET {sets} WHERE tenant_id = %s AND event_key = %s', (*[values[c] for c in cols], tenant_id, key))


def reconcile(conn, tenant_id: str, currency: str, desired: dict[str, dict[str, dict]], read_ok: dict[str, bool], today: dt.date) -> dict[str, Any]:
    """Apply the desired ledger rows (per table) on one connection. Returns counters."""
    from psycopg.rows import dict_row
    result = {'inserted': 0, 'updated': 0, 'removed': 0, 'skipped_removals': 0}
    window_start = today - dt.timedelta(days=WINDOW_DAYS)
    for table, wanted in desired.items():
        if not read_ok.get(table, False):
            continue
        cur = conn.cursor(row_factory=dict_row)
        existing = {r['event_key']: r for r in cur.execute(
            f"SELECT event_key, {', '.join(COMPARE[table])}{', status' if table == 'operations' else ''}, {DATE_COLUMN[table]} AS _day "
            f"FROM {table} WHERE tenant_id = %s", (tenant_id,)).fetchall()}
        for key, item in wanted.items():
            current = existing.get(key)
            if current is None:
                _insert(conn, table, tenant_id, key, item['values'], currency)
                result['inserted'] += 1
            elif not _same(table, current, item['values']) or (table == 'operations' and current['status'] != 'confirmed'):
                _update(conn, table, tenant_id, key, item['values'])
                result['updated'] += 1
        stale = [k for k, r in existing.items() if k not in wanted and (r['_day'] is None or r['_day'] >= window_start)
                 and (table != 'operations' or r['status'] == 'confirmed')]
        in_window = sum(1 for r in existing.values() if r['_day'] is None or r['_day'] >= window_start)
        if stale and len(stale) > max(10, MAX_STALE_SHARE * in_window):
            result['skipped_removals'] += len(stale)
            continue
        for key in stale:
            if table == 'operations':
                conn.execute("UPDATE operations SET status = 'rejected' WHERE tenant_id = %s AND event_key = %s", (tenant_id, key))
            else:
                conn.execute(f'DELETE FROM {table} WHERE tenant_id = %s AND event_key = %s', (tenant_id, key))
            result['removed'] += 1
    return result


def sync_tenant(tenant_id: str, cfg: dict[str, Any], *, database_url: str, gc, dry_run: bool = False, today: dt.date | None = None) -> dict[str, Any]:
    """Read the tenant's sheet and reconcile the PostgreSQL ledger with it."""
    import psycopg
    from notimate.tenants import cfg_currency

    currency = cfg_currency(cfg)
    sh = gc.open_by_key(cfg['sheet_id'])
    desired: dict[str, dict[str, dict]] = {t: {} for t in set(TAB_TABLE.values())}
    read_ok = {t: True for t in desired}
    tab_counts: dict[str, int] = {}
    for tab in TABS:
        table = TAB_TABLE[tab]
        try:
            rows = sh.worksheet(tab).get_all_records()
        except Exception as exc:
            if 'WorksheetNotFound' in type(exc).__name__:
                tab_counts[tab] = 0
                continue
            read_ok[table] = False  # a failed read must never trigger deletions
            tab_counts[tab] = -1
            continue
        planned = plan_tab(tab, rows, currency)
        tab_counts[tab] = len(planned)
        for item in planned:
            desired[table][item['key']] = item
    summary: dict[str, Any] = {'tabs': tab_counts}
    if dry_run:
        return {**summary, 'dry_run': True}
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            # Legacy keys from the first (row-number based) backfill — replaced by content keys.
            for table in desired:
                conn.execute(f"DELETE FROM {table} WHERE tenant_id = %s AND event_key LIKE 'backfill:%%'", (tenant_id,))
            summary.update(reconcile(conn, tenant_id, currency, desired, read_ok, today or dt.date.today()))
    return summary


def run_sync_safely(tenant_id: str, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Scheduled / on-demand entry point: never raises, logs a content-free summary."""
    import os

    import app
    from logging_utils import get_logger

    logger = get_logger()
    if os.environ.get('SHEETS_SYNC') == '0' or not (app.gc and app.DATABASE_URL and cfg.get('sheet_id')):
        return None
    try:
        summary = sync_tenant(tenant_id, cfg, database_url=app.DATABASE_URL, gc=app.gc)
        logger.info('sheets_sync_completed', extra={k: summary.get(k) for k in ('inserted', 'updated', 'removed', 'skipped_removals')})
        return summary
    except Exception as exc:
        logger.warning('sheets_sync_failed', extra={'error_type': type(exc).__name__})
        return None
