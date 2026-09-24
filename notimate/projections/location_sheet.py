"""«Отчёты точек» → вкладка Google Sheets «Отчёты точек» (owner-facing view of confirmed reports).

One row per confirmed report, appended idempotently (the draft id is the event key), best-effort:
a Sheets problem never blocks the confirmation the employee already got. PostgreSQL stays the
source for the dashboard; the tab is what the owner reads on a phone.
"""

from __future__ import annotations

from typing import Any

import app
from logging_utils import get_logger
from notimate.projections.sheets import append_rows_once, get_or_create_sheet

logger = get_logger()
TAB = 'Отчёты точек'
HEADERS = ['Дата', 'Точка', 'Выручка', 'Наличные', 'Безнал', 'Внешние выплаты', 'Остаток наличных', 'Комментарий', 'Сотрудник']


def _n(value: Any) -> Any:
    return '' if value is None else float(value)


def report_row(draft: dict[str, Any], location_name: str, staff_name: str) -> list[Any]:
    return [
        str(draft.get('occurred_on') or ''), location_name, _n(draft.get('revenue')), _n(draft.get('cash')), _n(draft.get('non_cash')),
        _n(draft.get('external_payouts')), _n(draft.get('cash_balance')), draft.get('comment') or '', staff_name or '',
    ]


def project_report_safely(sheet_id: str | None, draft: dict[str, Any], location_name: str, staff_name: str = '') -> None:
    if not (sheet_id and app.gc):
        return
    try:
        ws = get_or_create_sheet(app.gc.open_by_key(sheet_id), TAB, HEADERS)
        append_rows_once(ws, [report_row(draft, location_name, staff_name)], f"report-{draft['id']}", 'location-report')
    except Exception as exc:
        logger.warning('location_sheet_projection_failed', extra={'error_type': type(exc).__name__})
