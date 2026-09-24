"""Month package for the accountant (docs/19): registry + ZIP of originals named by number +
paper-envelope list + open questions. Pure functions over plain dicts; the caller supplies
``read_original`` so tests never touch a disk or database.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import os
import smtplib
import zipfile
from collections.abc import Callable
from email.message import EmailMessage
from typing import Any

from notimate.packs.accountant.checks import summarize_findings
from notimate.packs.accountant.rules import DOC_TYPES
from notimate.packs.accountant.storage import extension_of

REGISTRY_HEADER = ['№', 'Дата', 'Тип документа', 'Продавец', 'Налоговый номер', '№ документа', 'Сумма без НДС', 'НДС', 'Итого', 'Валюта', 'Оплата', 'Файл']


def _cell(value: Any) -> Any:
    """Neutralize spreadsheet formula injection: a seller name read off a photo must not
    be able to start with = + - @ and execute when the accountant opens the CSV."""
    if isinstance(value, str) and value[:1] in ('=', '+', '-', '@', '\t', '\r'):
        return "'" + value
    return '' if value is None else value


def _csv_bytes(rows: list[list[Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=';')
    for row in rows:
        writer.writerow([_cell(value) for value in row])
    return buffer.getvalue().encode('utf-8-sig')  # BOM so Excel opens Cyrillic/Thai correctly


def _fmt(value: Any) -> Any:
    if value is None:
        return ''
    number = float(value)
    return int(number) if number.is_integer() else round(number, 2)


def _filename(doc: dict[str, Any]) -> str:
    return f"{doc['doc_number']}.{extension_of(doc.get('storage_ref') or '')}"


def build_registry_csv(documents: list[dict[str, Any]]) -> bytes:
    rows: list[list[Any]] = [REGISTRY_HEADER]
    for doc in documents:
        rows.append([
            doc.get('doc_number'), str(doc.get('doc_date') or ''), DOC_TYPES.get(doc.get('doc_type'), doc.get('doc_type')),
            doc.get('seller'), doc.get('tax_id'), doc.get('doc_ref'), _fmt(doc.get('subtotal')), _fmt(doc.get('vat')),
            _fmt(doc.get('total')), doc.get('currency'), doc.get('payment_method'), _filename(doc) if doc.get('storage_ref') else '',
        ])
    return _csv_bytes(rows)


def build_originals_list_csv(documents: list[dict[str, Any]]) -> bytes:
    """List for the paper «конверт месяца»: the number written on each original."""
    rows: list[list[Any]] = [['№ на оригинале', 'Дата', 'Продавец', 'Итого', 'Тип документа', 'Отметка «оригинал в конверте»']]
    for doc in documents:
        rows.append([doc.get('doc_number'), str(doc.get('doc_date') or ''), doc.get('seller'), _fmt(doc.get('total')),
                     DOC_TYPES.get(doc.get('doc_type'), ''), ''])
    return _csv_bytes(rows)


def build_open_questions_text(findings: list[dict[str, Any]], questions: list[dict[str, Any]]) -> bytes:
    lines = [summarize_findings(findings, limit=500), '']
    if questions:
        lines.append(f'Вопросы бухгалтера ({len(questions)}):')
        for question in questions:
            ref = f" (№{question['doc_number']})" if question.get('doc_number') else ''
            lines.append(f"• {question['text']}{ref}")
    return '\n'.join(lines).encode('utf-8')


def build_month_package(
    tenant_name: str, period: str, documents: list[dict[str, Any]], findings: list[dict[str, Any]],
    questions: list[dict[str, Any]], read_original: Callable[[str], bytes],
) -> tuple[bytes, str]:
    """Return ``(zip_bytes, summary_text)`` for one period."""
    archive = io.BytesIO()
    missing_files: list[str] = []
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(f'{period}/реестр.csv', build_registry_csv(documents))
        bundle.writestr(f'{period}/список_оригиналов.csv', build_originals_list_csv(documents))
        for doc in documents:
            if not doc.get('storage_ref'):
                continue
            try:
                bundle.writestr(f'{period}/фото/{_filename(doc)}', read_original(doc['storage_ref']))
            except (OSError, ValueError):
                missing_files.append(str(doc.get('doc_number')))
        extra = list(findings)
        for number in missing_files:
            extra.append({'kind': 'missing_file', 'severity': 'warning', 'doc_number': number,
                          'text': f'Файл фото для №{number} не найден в хранилище — запросите повторно.'})
        bundle.writestr(f'{period}/открытые_вопросы.txt', build_open_questions_text(extra, questions))
    total = sum(float(d['total']) for d in documents if d.get('total') is not None)
    summary = (
        f'📦 Пакет за {period} — {tenant_name}\n'
        f'Документов: {len(documents)}, на сумму {total:,.0f}'.replace(',', ' ') + '\n'
        f'Открытых вопросов: {len(findings) + len(missing_files) + len(questions)}\n'
        'В архиве: реестр, список оригиналов для конверта, фото по номерам, открытые вопросы.'
    )
    return archive.getvalue(), summary


def previous_period(today: dt.date) -> str:
    first = today.replace(day=1)
    return (first - dt.timedelta(days=1)).strftime('%Y-%m')


def send_email(to: list[str], subject: str, body: str, filename: str, data: bytes) -> bool:
    """Send the package by e-mail when SMTP_* is configured; returns False (no-op) when it isn't."""
    host = os.environ.get('SMTP_HOST')
    sender = os.environ.get('SMTP_FROM')
    if not (host and sender and to):
        return False
    message = EmailMessage()
    message['From'], message['To'], message['Subject'] = sender, ', '.join(to), subject
    message.set_content(body)
    message.add_attachment(data, maintype='application', subtype='zip', filename=filename)
    with smtplib.SMTP(host, int(os.environ.get('SMTP_PORT', '587')), timeout=30) as smtp:
        smtp.starttls()
        if os.environ.get('SMTP_USER'):
            smtp.login(os.environ['SMTP_USER'], os.environ.get('SMTP_PASSWORD', ''))
        smtp.send_message(message)
    return True
