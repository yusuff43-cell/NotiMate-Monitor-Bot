#!/usr/bin/env python3
"""Load a client's existing Google Sheets history into the PostgreSQL ledger.

The ledger only started filling on 2026-09-23 (Этап 4 dual-write), so the web dashboard
would otherwise show a nearly empty history for clients with months of data in Sheets.
This copies the rows of «Выручка», «Расходы», «Зарплаты», «Закупки», «Остатки», «Проблемы»
and «Напоминания» into ``operations`` / ``stock_signals`` / ``issues`` / ``reminders``.

Idempotent: a row that already carries the technical «NotiMate Event ID» reuses that exact
key (so it can never duplicate what the dual-write already stored); older rows get a
stable ``backfill:<tab>:<row number>`` key. Re-running writes nothing new. Read-only on
the sheet.

    docker compose -f compose.vps.yml exec -T worker python deploy/backfill_from_sheets.py --tenant <LINE destination> --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['DISABLE_SCHEDULER'] = '1'

EVENT_ID_HEADER = 'NotiMate Event ID'


def parse_date(value) -> str | None:
    try:
        return dt.date.fromisoformat(str(value).strip()[:10]).isoformat()
    except ValueError:
        return None


def key_for(row: dict, tab: str, number: int) -> str:
    return str(row.get(EVENT_ID_HEADER) or '').strip() or f'backfill:{tab}:{number}'


def amount_column(row: dict) -> float | None:
    for name, value in row.items():
        if str(name).startswith('Сумма (') and str(value).strip() != '':
            return money(value)
    return None


def money(value) -> float:
    from notimate.projections.sheets import _money
    return _money(value)


def plan(tab: str, rows: list[dict], currency: str) -> list[tuple[str, tuple]]:
    """Translate one tab's rows into ``(store_method, args-after-tenant)`` calls."""
    calls: list[tuple[str, tuple]] = []
    last_day = None
    for number, row in enumerate(rows, start=2):
        day = parse_date(row.get('Дата') or row.get('Дата добавления'))
        if tab in ('Закупки', 'Остатки'):
            # These tabs write the date on the first row of a group only; the rows below
            # belong to the same day until the next date appears.
            day = day or last_day
            last_day = day
        key = key_for(row, tab, number)
        if tab == 'Выручка' and day:
            details = {k: row.get(h) for k, h in (('cash', 'Наличные'), ('card', 'Карта'), ('qr', 'QR')) if row.get(h) not in (None, '')}
            calls.append(('record_operation', (key, 'revenue', day, money(row.get('Gross Sales')), currency, None, f"Смена {row.get('Смена', '')}", details)))
        elif tab == 'Расходы' and day:
            calls.append(('record_operation', (key, 'expense', day, amount_column(row), currency, row.get('Поставщик/Магазин') or None, row.get('Позиция') or '', None)))
        elif tab == 'Зарплаты' and day:
            calls.append(('record_operation', (key, 'salary', day, amount_column(row), currency, row.get('Получатель') or None, row.get('Примечание') or '', None)))
        elif tab == 'Закупки' and day:
            calls.append(('record_operation', (key, 'purchase', day, None, currency, None, row.get('Продукт') or '', {'quantity': row.get('Количество', '')})))
        elif tab == 'Остатки' and day and row.get('Продукт'):
            calls.append(('record_stock_signal', (key, day, row.get('Категория') or '', row['Продукт'], str(row.get('Холодильник', '')), str(row.get('Морозилка', '')), row.get('Примечание') or '')))
        elif tab == 'Проблемы' and day:
            calls.append(('record_issue', (key, day, str(row.get('Сообщение', ''))[:2000], str(row.get('Перевод и совет', ''))[:2000])))
        elif tab == 'Напоминания' and row.get('Название'):
            calls.append(('record_reminder', (key, row['Название'], parse_date(row.get('Дата окончания')), parse_date(row.get('Дата добавления')) or dt.date.today().isoformat(), row.get('Примечание') or '')))
    return calls


TABS = ('Выручка', 'Расходы', 'Зарплаты', 'Закупки', 'Остатки', 'Проблемы', 'Напоминания')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--tenant', required=True, help='tenant id (the LINE destination for JSC)')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    import app
    cfg = app.find_client(args.tenant)
    if not (cfg and app.gc and app.operations_store):
        sys.exit('Need a known tenant, Google credentials and DATABASE_URL')
    from notimate.tenants import cfg_currency
    currency = cfg_currency(cfg)
    sh = app.gc.open_by_key(cfg['sheet_id'])
    total_new = 0
    for tab in TABS:
        try:
            rows = sh.worksheet(tab).get_all_records()
        except Exception:
            print(f'{tab}: no tab, skipped')
            continue
        calls = plan(tab, rows, currency)
        new = 0
        for method, call_args in calls:
            if args.dry_run:
                continue
            if getattr(app.operations_store, method)(args.tenant, *call_args):
                new += 1
        total_new += new
        print(f'{tab}: {len(rows)} rows, {len(calls)} importable' + ('' if args.dry_run else f', {new} new'))
    print('dry run: nothing written' if args.dry_run else f'done: {total_new} new rows')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
