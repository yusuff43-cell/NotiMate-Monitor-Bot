"""Completeness checks for the month package — plain code, no model (docs/19: «Где AI, а где код»).

Inputs are plain dicts so the checks stay unit-testable without PostgreSQL:
* documents: confirmed rows of the ``documents`` table (``id``, ``doc_number``, ``doc_type``,
  ``seller``, ``tax_id``, ``doc_ref``, ``doc_date`` (ISO str), ``subtotal``, ``vat``,
  ``total``, ``payment_method``, ``image_sha256``, ``confidence``);
* operations: rows of the ``operations`` table (``id``, ``operation_type``, ``occurred_on``,
  ``amount``, ``counterparty``, ``description``).

Every finding is ``{'kind', 'severity', 'text', 'doc_number'?, 'operation_id'?}`` with a
ready-to-send Russian text (the docs/19 examples: «Нужен полный tax invoice от Makro …»).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from notimate.packs.accountant.rules import FORMAL_DOC_TYPES

EXPENSE_OPERATION_TYPES = ('expense', 'purchase')


def _num(value: Any) -> float | None:
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _money(value: float | None, currency: str = '') -> str:
    if value is None:
        return '—'
    return f'{value:,.0f}'.replace(',', ' ') + (f' {currency}' if currency else '')


def link_documents_to_operations(
    documents: list[dict[str, Any]], operations: list[dict[str, Any]], rules: dict[str, Any],
) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    """Greedy one-to-one match by amount (within tolerance) and date (within the window).

    Returns ``(linked_pairs, unmatched_documents, unmatched_expense_operations)``. Only
    expense-like operations take part; revenue/salary never need a supplier document.
    """
    tolerance = float(rules.get('amount_tolerance', 1.0))
    window = int(rules.get('date_window_days', 3))
    expenses = [op for op in operations if op.get('operation_type') in EXPENSE_OPERATION_TYPES]
    free_ops = list(expenses)
    linked: list[tuple[dict, dict]] = []
    unmatched_docs: list[dict] = []
    for doc in sorted(documents, key=lambda d: (str(d.get('doc_date') or ''), str(d.get('doc_number') or ''))):
        total, doc_date = _num(doc.get('total')), _date(doc.get('doc_date'))
        best, best_gap = None, None
        for op in free_ops:
            amount, op_date = _num(op.get('amount')), _date(op.get('occurred_on'))
            if total is None or amount is None or abs(total - amount) > tolerance:
                continue
            gap = abs((doc_date - op_date).days) if doc_date and op_date else window
            if gap > window:
                continue
            if best is None or gap < best_gap:
                best, best_gap = op, gap
        if best is None:
            unmatched_docs.append(doc)
        else:
            free_ops.remove(best)
            linked.append((doc, best))
    return linked, unmatched_docs, free_ops


def find_duplicates(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    by_hash: dict[str, dict] = {}
    by_key: dict[tuple, dict] = {}
    for doc in sorted(documents, key=lambda d: str(d.get('doc_number') or '')):
        sha = doc.get('image_sha256')
        key = (str(doc.get('tax_id') or doc.get('seller') or '').strip().lower(), str(doc.get('doc_ref') or '').strip(), _num(doc.get('total')))
        first = by_hash.get(sha) if sha else None
        if first is None and key[0] and key[1]:
            first = by_key.get(key)
        if first is not None:
            findings.append({
                'kind': 'duplicate', 'severity': 'warning', 'doc_number': doc.get('doc_number'),
                'text': f"Возможный дубль: №{doc.get('doc_number')} похож на №{first.get('doc_number')} ({doc.get('seller') or 'без названия'}, {_money(_num(doc.get('total')))}).",
            })
            continue
        if sha:
            by_hash[sha] = doc
        if key[0] and key[1]:
            by_key[key] = doc
    return findings


def check_month(
    documents: list[dict[str, Any]], operations: list[dict[str, Any]], rules: dict[str, Any],
    *, compare_operations: bool = True,
) -> list[dict[str, Any]]:
    """All findings for one period. ``compare_operations`` is False for tenants whose
    documents are the only source of expenses (no operations feed to reconcile against)."""
    currency = str(rules.get('currency') or '')
    findings: list[dict[str, Any]] = []

    for doc in documents:
        number, seller = doc.get('doc_number'), doc.get('seller') or 'без названия'
        total, doc_type = _num(doc.get('total')), doc.get('doc_type')
        when = str(doc.get('doc_date') or '')[:10]
        vat = _num(doc.get('vat'))
        min_full = rules.get('full_invoice_min_total')
        needs_full = (
            rules.get('vat_registered') and min_full is not None
            and doc_type == 'receipt_simplified' and (total or 0) >= float(min_full)
        )
        if needs_full:
            findings.append({
                'kind': 'missing_full_tax_invoice', 'severity': 'action', 'doc_number': number,
                'text': f'Нужен полный tax invoice от {seller} за {when}, {_money(total, currency)} (№{number}).',
            })
        min_formal = rules.get('formal_doc_min_total')
        if min_formal is not None and doc_type not in FORMAL_DOC_TYPES and (total or 0) >= float(min_formal):
            findings.append({
                'kind': 'missing_formal_document', 'severity': 'action', 'doc_number': number,
                'text': f'Для №{number} ({seller}, {_money(total, currency)}) нужен формальный документ (накладная/счёт-фактура).',
            })
        subtotal = _num(doc.get('subtotal'))
        if subtotal is not None and vat is not None and total is not None:
            if abs(subtotal + vat - total) > max(float(rules.get('amount_tolerance', 1.0)), total * 0.005):
                findings.append({
                    'kind': 'amount_inconsistent', 'severity': 'warning', 'doc_number': number,
                    'text': f'В №{number} ({seller}) сумма без НДС + НДС не равна итогу — проверьте фото.',
                })
        if total is None or not doc.get('doc_date'):
            findings.append({
                'kind': 'incomplete_fields', 'severity': 'warning', 'doc_number': number,
                'text': f'В №{number} ({seller}) не распознаны сумма или дата — уточните по оригиналу.',
            })

    findings.extend(find_duplicates(documents))

    if rules.get('transfer_needs_invoice'):
        invoices = [d for d in documents if d.get('doc_type') in FORMAL_DOC_TYPES]
        for slip in (d for d in documents if d.get('doc_type') == 'bank_slip'):
            total, sd = _num(slip.get('total')), _date(slip.get('doc_date'))
            has_invoice = any(
                total is not None and _num(inv.get('total')) is not None
                and abs(_num(inv['total']) - total) <= float(rules.get('amount_tolerance', 1.0))
                for inv in invoices
            ) or any(
                slip.get('seller') and inv.get('seller') and str(slip['seller']).lower() == str(inv['seller']).lower()
                for inv in invoices
            )
            if not has_invoice:
                findings.append({
                    'kind': 'transfer_without_invoice', 'severity': 'action', 'doc_number': slip.get('doc_number'),
                    'text': f"Перевод №{slip.get('doc_number')} ({slip.get('seller') or 'получатель не указан'}, {_money(total, currency)}, {sd or ''}) без счёта/накладной — приложите документ основания.",
                })

    if compare_operations:
        linked, unmatched_docs, unmatched_ops = link_documents_to_operations(documents, operations, rules)
        for op in unmatched_ops:
            findings.append({
                'kind': 'expense_without_document', 'severity': 'action', 'operation_id': op.get('id'),
                'text': f"Нет документа на расход: {op.get('counterparty') or op.get('description') or 'без описания'}, {_money(_num(op.get('amount')), currency)}, {str(op.get('occurred_on'))[:10]}.",
            })
        for doc in unmatched_docs:
            if doc.get('doc_type') == 'bank_slip':
                continue
            findings.append({
                'kind': 'document_without_expense', 'severity': 'info', 'doc_number': doc.get('doc_number'),
                'text': f"Документ №{doc.get('doc_number')} ({doc.get('seller') or 'без названия'}, {_money(_num(doc.get('total')), currency)}) не связан ни с одним расходом.",
            })
    return findings


def summarize_findings(findings: list[dict[str, Any]], limit: int = 15) -> str:
    if not findings:
        return '✅ Всё в порядке: недостающих документов не найдено.'
    order = {'action': 0, 'warning': 1, 'info': 2}
    ordered = sorted(findings, key=lambda f: order.get(f.get('severity'), 3))
    lines = [f'📎 Не хватает / под вопросом: {len(findings)}']
    lines += [f"• {f['text']}" for f in ordered[:limit]]
    if len(ordered) > limit:
        lines.append(f'… и ещё {len(ordered) - limit}')
    return '\n'.join(lines)
