"""Read-only PostgreSQL aggregates behind ``GET /v1/owner-dashboard`` (docs/17 contract).

Everything is scoped by ``tenant_id`` in SQL — the caller passes the tenant from the
verified session, never from request input. Sources: the Этап 4 ledger (operations,
stock_signals, reminders, issues) and, for «Отчёты точек» tenants, ``location_reports``.
``expenses`` follows the same definition as the LINE evening summary (rows of the
«Расходы» sheet = ``expense`` operations); salaries are reported separately so numbers
stay comparable with what owners already see.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from notimate.dashboard.categories import categorize

CRITICAL_NOTES = {'Out of stock': 'out', 'Exp today': 'expiry', 'Low stock': 'low'}
TREND_DAYS = {'day': 14, 'week': 14, 'month': 30}
CURRENCIES = {'TH': 'THB', 'KZ': 'KZT'}


def _driver():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg, dict_row


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


class PostgresDashboardReader:
    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError('DATABASE_URL is required')
        self.database_url = database_url

    def _rows(self, sql: str, params: tuple) -> list[dict[str, Any]]:
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def daily_totals(self, tenant_id: str, start: dt.date, end: dt.date, location_pack: bool) -> dict[str, dict[str, float]]:
        """``{iso_date: {'revenue': x, 'expenses': y}}`` between start and end inclusive."""
        if location_pack:
            rows = self._rows(
                """
                SELECT occurred_on AS day, COALESCE(SUM(revenue), 0) AS revenue, COALESCE(SUM(external_payouts), 0) AS expenses
                FROM location_reports WHERE tenant_id = %s AND occurred_on BETWEEN %s AND %s GROUP BY occurred_on
                """,
                (tenant_id, start, end),
            )
        else:
            rows = self._rows(
                """
                SELECT occurred_on AS day,
                       COALESCE(SUM(amount) FILTER (WHERE operation_type = 'revenue'), 0) AS revenue,
                       COALESCE(SUM(amount) FILTER (WHERE operation_type = 'expense'), 0) AS expenses
                FROM operations
                WHERE tenant_id = %s AND status = 'confirmed' AND occurred_on BETWEEN %s AND %s
                GROUP BY occurred_on
                """,
                (tenant_id, start, end),
            )
        return {row['day'].isoformat(): {'revenue': _f(row['revenue']), 'expenses': _f(row['expenses'])} for row in rows}

    def payments_for(self, tenant_id: str, day: dt.date, location_pack: bool) -> dict[str, float]:
        if location_pack:
            rows = self._rows(
                'SELECT COALESCE(SUM(cash), 0) AS cash, COALESCE(SUM(non_cash), 0) AS card FROM location_reports WHERE tenant_id = %s AND occurred_on = %s',
                (tenant_id, day),
            )
            return {'cash': _f(rows[0]['cash']), 'card': _f(rows[0]['card']), 'qr': 0.0}
        totals = {'cash': 0.0, 'card': 0.0, 'qr': 0.0}
        for row in self._rows(
            "SELECT details FROM operations WHERE tenant_id = %s AND operation_type = 'revenue' AND status = 'confirmed' AND occurred_on = %s",
            (tenant_id, day),
        ):
            details = row['details'] or {}
            for key in totals:
                totals[key] += _f(details.get(key))
        return totals

    def critical_stock(self, tenant_id: str, since: dt.date) -> list[dict[str, str]]:
        rows = self._rows(
            """
            SELECT product, fridge, freezer, note FROM stock_signals
            WHERE tenant_id = %s AND occurred_on >= %s AND note = ANY(%s)
            ORDER BY occurred_on DESC, id DESC
            """,
            (tenant_id, since, list(CRITICAL_NOTES)),
        )
        seen, out = set(), []
        for row in rows:
            if row['product'] in seen:
                continue
            seen.add(row['product'])
            amount = ' / '.join(part for part in (row.get('fridge'), row.get('freezer')) if part)
            out.append({'product': row['product'], 'amount': amount or '—', 'status': CRITICAL_NOTES[row['note']]})
        return out[:10]

    def deadlines(self, tenant_id: str, today: dt.date, days: int = 14) -> list[dict[str, Any]]:
        rows = self._rows(
            'SELECT title, expiry_date FROM reminders WHERE tenant_id = %s AND expiry_date BETWEEN %s AND %s ORDER BY expiry_date LIMIT 10',
            (tenant_id, today, today + dt.timedelta(days=days)),
        )
        return [{'title': r['title'], 'date': r['expiry_date'].isoformat(), 'daysLeft': (r['expiry_date'] - today).days} for r in rows]

    def problems(self, tenant_id: str, since: dt.date) -> list[dict[str, str]]:
        rows = self._rows(
            'SELECT message FROM issues WHERE tenant_id = %s AND occurred_on >= %s ORDER BY occurred_on DESC, id DESC LIMIT 5',
            (tenant_id, since),
        )
        return [{'title': (r['message'] or '')[:120], 'status': 'Новая'} for r in rows]

    def expense_rows(self, tenant_id: str, start: dt.date, end: dt.date, location_pack: bool) -> list[dict[str, Any]]:
        """Expense-like rows (expense + salary) in [start, end], newest first, for the by-category view.
        «Отчёты точек» tenants have no itemised expenses (только «внешние выплаты» за день), so they return []."""
        if location_pack:
            return []
        return self._rows(
            """
            SELECT occurred_on AS day, operation_type, counterparty, description, amount
            FROM operations
            WHERE tenant_id = %s AND status = 'confirmed' AND operation_type IN ('expense', 'salary')
              AND amount IS NOT NULL AND occurred_on BETWEEN %s AND %s
            ORDER BY occurred_on DESC, id DESC LIMIT 2000
            """,
            (tenant_id, start, end),
        )

    def location_rows(self, tenant_id: str, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
        """Per-location totals for «Отчёты точек» tenants (every active location, reported or not)."""
        return self._rows(
            """
            SELECT l.id, l.name,
                   COALESCE(SUM(r.revenue), 0) AS revenue, COALESCE(SUM(r.cash), 0) AS cash,
                   COALESCE(SUM(r.non_cash), 0) AS non_cash, COALESCE(SUM(r.external_payouts), 0) AS payouts,
                   COUNT(r.id) AS reports
            FROM locations l
            LEFT JOIN location_reports r ON r.location_id = l.id AND r.occurred_on BETWEEN %s AND %s
            WHERE l.tenant_id = %s AND l.status = 'active'
            GROUP BY l.id, l.name ORDER BY l.name
            """,
            (start, end, tenant_id),
        )

    def recent_operations(self, tenant_id: str, location_pack: bool) -> list[dict[str, Any]]:
        if location_pack:
            rows = self._rows(
                """
                SELECT l.name AS label, r.revenue AS amount, 'revenue' AS kind
                FROM location_reports r JOIN locations l ON l.id = r.location_id
                WHERE r.tenant_id = %s ORDER BY r.confirmed_at DESC LIMIT 8
                """,
                (tenant_id,),
            )
        else:
            rows = self._rows(
                """
                SELECT COALESCE(NULLIF(description, ''), NULLIF(counterparty, ''), operation_type) AS label,
                       amount, CASE WHEN operation_type = 'revenue' THEN 'revenue' ELSE 'expense' END AS kind
                FROM operations
                WHERE tenant_id = %s AND status = 'confirmed' AND operation_type IN ('revenue', 'expense', 'salary') AND amount IS NOT NULL
                ORDER BY created_at DESC, id DESC LIMIT 8
                """,
                (tenant_id,),
            )
        return [{'label': r['label'], 'amount': _f(r['amount']), 'kind': r['kind']} for r in rows]


def build_snapshot(reader, tenant: dict[str, Any], period: str, now: dt.datetime, accounting: dict[str, Any] | None = None) -> dict[str, Any]:
    """Assemble the docs/17 ``DashboardSnapshot`` for one tenant at local time ``now``."""
    tenant_id = tenant['id']
    location_pack = tenant.get('vertical_pack') == 'location_reports'
    today = now.date()
    trend_days = TREND_DAYS.get(period, 14)
    start = min(today.replace(day=1), today - dt.timedelta(days=trend_days - 1))
    totals = reader.daily_totals(tenant_id, start, today, location_pack)

    def sum_range(first: dt.date, key: str) -> float:
        return sum(v[key] for day, v in totals.items() if first.isoformat() <= day <= today.isoformat())

    month_start = today.replace(day=1)
    today_rev, today_exp = totals.get(today.isoformat(), {}).get('revenue', 0.0), totals.get(today.isoformat(), {}).get('expenses', 0.0)
    month_rev, month_exp = sum_range(month_start, 'revenue'), sum_range(month_start, 'expenses')
    payments = reader.payments_for(tenant_id, today, location_pack)
    trend = []
    for offset in range(trend_days - 1, -1, -1):
        day = (today - dt.timedelta(days=offset)).isoformat()
        values = totals.get(day, {})
        trend.append({'date': day, 'revenue': values.get('revenue', 0.0), 'expenses': values.get('expenses', 0.0)})
    # Selected period (the dashboard's «День / Неделя / Месяц» switch)
    if period == 'week':
        sel_from = today - dt.timedelta(days=6)
    elif period == 'month':
        sel_from = month_start
    else:
        sel_from = today
    sel_totals = reader.daily_totals(tenant_id, sel_from, today, location_pack) if sel_from < start else {
        d: v for d, v in totals.items() if sel_from.isoformat() <= d <= today.isoformat()}
    sel_rev = sum(v['revenue'] for v in sel_totals.values())
    sel_exp = sum(v['expenses'] for v in sel_totals.values())

    overrides = (tenant.get('modules') or {}).get('expense_categories') if isinstance(tenant.get('modules'), dict) else None
    by_category: dict[str, dict[str, Any]] = {}
    expense_list = []
    for row in reader.expense_rows(tenant_id, sel_from, today, location_pack):
        amount = _f(row['amount'])
        category = categorize(row.get('counterparty'), row.get('description'), row['operation_type'], overrides)
        slot = by_category.setdefault(category, {'category': category, 'amount': 0.0, 'count': 0})
        slot['amount'] += amount
        slot['count'] += 1
        if len(expense_list) < 200:
            expense_list.append({
                'date': row['day'].isoformat(), 'label': (row.get('description') or row.get('counterparty') or '—')[:120],
                'supplier': (row.get('counterparty') or '')[:80], 'category': category, 'amount': amount,
            })
    spent = sum(c['amount'] for c in by_category.values())
    categories = sorted(by_category.values(), key=lambda c: -c['amount'])
    for entry in categories:
        entry['share'] = round(entry['amount'] / spent, 4) if spent else 0.0

    locations = []
    if location_pack:
        for row in reader.location_rows(tenant_id, sel_from, today):
            locations.append({
                'name': row['name'], 'revenue': _f(row['revenue']), 'cash': _f(row['cash']), 'nonCash': _f(row['non_cash']),
                'payouts': _f(row['payouts']), 'reports': int(row['reports'] or 0),
            })

    return {
        'channels': list(tenant.get('channels') or []),
        'tenantName': tenant.get('name') or '',
        'sheetUrl': f"https://docs.google.com/spreadsheets/d/{tenant['sheet_id']}" if tenant.get('sheet_id') else None,
        'selected': {'period': period, 'from': sel_from.isoformat(), 'to': today.isoformat(), 'revenue': sel_rev, 'expenses': sel_exp, 'result': sel_rev - sel_exp},
        'expensesByCategory': categories,
        'expenses': expense_list,
        'locations': locations,
        'accounting': accounting,
        'generatedAt': now.isoformat(),
        'timezone': tenant.get('timezone') or 'Asia/Bangkok',
        'currency': CURRENCIES.get((tenant.get('country') or '').upper(), 'THB'),
        'period': period,
        'today': {'revenue': today_rev, 'expenses': today_exp, 'result': today_rev - today_exp},
        'month': {'revenue': month_rev, 'expenses': month_exp, 'result': month_rev - month_exp},
        'payments': [{'label': label, 'amount': payments[label]} for label in ('cash', 'card', 'qr')],
        'trend': trend,
        'criticalStock': reader.critical_stock(tenant_id, today - dt.timedelta(days=3)),
        'deadlines': reader.deadlines(tenant_id, today),
        'problems': reader.problems(tenant_id, today - dt.timedelta(days=7)),
        'recentOperations': reader.recent_operations(tenant_id, location_pack),
    }


def build_group_snapshot(reader, tenants: list[dict[str, Any]], period: str, group_key: str, clock) -> dict[str, Any]:
    """Consolidated view over the businesses of one owner group.

    Each business is computed on its own (own timezone, own currency, own data); only then are
    they put side by side. Money is summed ONLY within the same currency — a baht business and a
    tenge business are never added together — and every line names its business.
    """
    businesses: list[dict[str, Any]] = []
    totals: dict[str, dict[str, float]] = {}
    for tenant in tenants:
        snap = build_snapshot(reader, tenant, period, clock(tenant.get('timezone')))
        currency = snap['currency']
        businesses.append({
            'id': tenant['id'], 'name': tenant.get('name') or tenant['id'], 'currency': currency, 'channels': snap['channels'],
            'selected': snap['selected'], 'today': snap['today'], 'month': snap['month'], 'points': len(snap['locations']),
        })
        slot = totals.setdefault(currency, {'revenue': 0.0, 'expenses': 0.0, 'monthRevenue': 0.0, 'monthExpenses': 0.0})
        slot['revenue'] += snap['selected']['revenue']
        slot['expenses'] += snap['selected']['expenses']
        slot['monthRevenue'] += snap['month']['revenue']
        slot['monthExpenses'] += snap['month']['expenses']
    return {
        'group': group_key,
        'period': period,
        'businesses': businesses,
        'totalsByCurrency': [
            {'currency': cur, 'revenue': v['revenue'], 'expenses': v['expenses'], 'result': v['revenue'] - v['expenses'],
             'monthRevenue': v['monthRevenue'], 'monthExpenses': v['monthExpenses'], 'monthResult': v['monthRevenue'] - v['monthExpenses']}
            for cur, v in sorted(totals.items())
        ],
    }
