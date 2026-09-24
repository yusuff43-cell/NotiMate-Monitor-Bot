#!/usr/bin/env python3
"""Этап 4 acceptance check: does the PostgreSQL ledger match the Google Sheets numbers?

docs/21: «вечерняя сводка JSC из PostgreSQL совпадает с той, что строилась из Sheets, на
данных за неделю». For each of the last N days this prints revenue and expenses from both
sources and exits 1 on any difference beyond 0.5 THB. Read-only: it writes nothing anywhere.
Only when a full week matches, set ``REPORTS_SOURCE=postgres`` in the server .env.

Run inside the worker container (it has DATABASE_URL and Google credentials):

    docker compose -f compose.vps.yml exec -T worker python deploy/compare_sheets_vs_postgres.py --tenant <LINE destination> --days 7
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['DISABLE_SCHEDULER'] = '1'

import app  # noqa: E402
from notimate.projections.sheets import _money  # noqa: E402
from notimate.timeutil import bangkok_now  # noqa: E402


def sheet_totals(sheet_id: str, days: list[str]) -> dict[str, dict[str, float]]:
    sh = app.gc.open_by_key(sheet_id)
    totals = {day: {'revenue': 0.0, 'expenses': 0.0} for day in days}
    for row in sh.worksheet('Выручка').get_all_records():
        day = str(row.get('Дата', ''))[:10]
        if day in totals:
            totals[day]['revenue'] += _money(row.get('Gross Sales'))
    for row in sh.worksheet('Расходы').get_all_records():
        day = str(row.get('Дата', ''))[:10]
        if day in totals:
            totals[day]['expenses'] += _money(row.get('Сумма (THB)'))
    return totals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--tenant', required=True, help='tenant id (the LINE destination for JSC)')
    parser.add_argument('--days', type=int, default=7)
    args = parser.parse_args()

    cfg = app.find_client(args.tenant)
    if not cfg or not app.gc or not app.dashboard_reader:
        sys.exit('Need a known tenant, Google credentials and DATABASE_URL')
    today = bangkok_now().date()
    days = [(today - dt.timedelta(days=i)).isoformat() for i in range(args.days - 1, -1, -1)]
    sheets = sheet_totals(cfg['sheet_id'], days)
    postgres = app.dashboard_reader.daily_totals(args.tenant, today - dt.timedelta(days=args.days - 1), today, False)

    mismatches = 0
    print(f"{'date':<12}{'rev sheets':>12}{'rev pg':>12}{'exp sheets':>12}{'exp pg':>12}  ok")
    for day in days:
        pg = postgres.get(day, {'revenue': 0.0, 'expenses': 0.0})
        ok = abs(sheets[day]['revenue'] - pg['revenue']) <= 0.5 and abs(sheets[day]['expenses'] - pg['expenses']) <= 0.5
        mismatches += 0 if ok else 1
        print(f"{day:<12}{sheets[day]['revenue']:>12.0f}{pg['revenue']:>12.0f}{sheets[day]['expenses']:>12.0f}{pg['expenses']:>12.0f}  {'ok' if ok else 'DIFF'}")
    print('MATCH' if not mismatches else f'{mismatches} day(s) differ — do not switch REPORTS_SOURCE yet')
    return 1 if mismatches else 0


if __name__ == '__main__':
    raise SystemExit(main())
