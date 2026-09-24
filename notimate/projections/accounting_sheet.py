"""Accounting tabs in the client's Google Sheet (owner-facing, short by design).

* «Бухгалтерия» — one row per numbered document (the same registry the accountant receives
  as CSV in the month package), so the owner can look up a document from a phone in seconds.
* «Не хватает» — the current list of missing / questionable documents, rewritten on each
  refresh (it is a status view, not a log).

Both are projections of PostgreSQL (``documents``); the writes are best-effort and never
raise, exactly like the other Sheets projections, and re-projecting a document is
idempotent through the shared ``NotiMate Event ID`` column.
"""

from __future__ import annotations

from typing import Any

import app
from logging_utils import get_logger
from notimate.packs.accountant.rules import DOC_TYPES
from notimate.projections.sheets import append_rows_once, get_or_create_sheet

logger = get_logger()

REGISTRY_TAB = 'Бухгалтерия'
MISSING_TAB = 'Не хватает'
REGISTRY_HEADERS = ['№', 'Дата документа', 'Тип', 'Продавец', 'Налоговый №', '№ документа', 'Сумма без НДС', 'НДС', 'Итого', 'Валюта', 'Оплата']
MISSING_HEADERS = ['Важность', 'Что не хватает / вопрос']
SEVERITY_LABEL = {'action': 'Нужно действие', 'warning': 'Проверить', 'info': 'К сведению'}


def _num(value: Any) -> Any:
    return '' if value is None else float(value)


def document_row(doc: dict[str, Any]) -> list[Any]:
    return [
        doc.get('doc_number'), str(doc.get('doc_date') or ''), DOC_TYPES.get(doc.get('doc_type'), doc.get('doc_type') or ''),
        doc.get('seller') or '', doc.get('tax_id') or '', doc.get('doc_ref') or '', _num(doc.get('subtotal')), _num(doc.get('vat')),
        _num(doc.get('total')), doc.get('currency') or '', doc.get('payment_method') or '',
    ]


def project_document(sheet_id: str, doc: dict[str, Any]) -> int:
    sh = app.gc.open_by_key(sheet_id)
    ws = get_or_create_sheet(sh, REGISTRY_TAB, REGISTRY_HEADERS)
    return append_rows_once(ws, [document_row(doc)], f"doc-{doc['id']}", 'registry')


def missing_rows(findings: list[dict[str, Any]]) -> list[list[str]]:
    order = {'action': 0, 'warning': 1, 'info': 2}
    ordered = sorted(findings, key=lambda f: order.get(f.get('severity'), 3))
    return [[SEVERITY_LABEL.get(f.get('severity'), ''), f.get('text', '')] for f in ordered]


def refresh_missing(sheet_id: str, findings: list[dict[str, Any]]) -> None:
    sh = app.gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(MISSING_TAB)
    except Exception:
        ws = sh.add_worksheet(title=MISSING_TAB, rows=200, cols=len(MISSING_HEADERS))
    rows = [MISSING_HEADERS] + (missing_rows(findings) or [['', '✅ Всё в порядке — недостающих документов не найдено.']])
    ws.clear()
    ws.update(values=rows, range_name='A1', value_input_option='USER_ENTERED')


def project_document_safely(sheet_id: str | None, doc: dict[str, Any]) -> None:
    if not (sheet_id and app.gc):
        return
    try:
        project_document(sheet_id, doc)
    except Exception as exc:
        logger.warning('accounting_sheet_projection_failed', extra={'error_type': type(exc).__name__})


def refresh_missing_safely(sheet_id: str | None, findings: list[dict[str, Any]]) -> None:
    if not (sheet_id and app.gc):
        return
    try:
        refresh_missing(sheet_id, findings)
    except Exception as exc:
        logger.warning('accounting_missing_sheet_failed', extra={'error_type': type(exc).__name__})
