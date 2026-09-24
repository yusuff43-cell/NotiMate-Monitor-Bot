"""Document classification and field extraction from a photo (the only AI step of the
module — numbering, linking and completeness checks are code, docs/19).

The model returns a strict JSON schema plus its own ``confidence``; whether a document is
saved straight away or shown as a draft is decided by ``document_is_confident`` (model
confidence AND deterministic arithmetic/field checks) through notimate/policy.py.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from notimate.packs.accountant.rules import DOC_TYPES, PAYMENT_METHODS

DOCUMENT_PROMPT = """Ты извлекаешь реквизиты бухгалтерского документа с фото для бухгалтера. Документ может быть на тайском, английском, русском или казахском языке; все пояснения возвращай на русском, названия продавца оставляй как на документе.

Определи тип (doc_type): tax_invoice (полный tax invoice / счёт-фактура / ЭСФ с налоговым номером покупателя и продавца), receipt_simplified (упрощённый чек, кассовый чек, abbreviated tax invoice), supplier_invoice (счёт поставщика на оплату), bank_slip (банковский слип, подтверждение перевода), delivery_note (накладная, АВР, акт), other (другое финансовое).

Верни ТОЛЬКО JSON:
{"doc_type":"...","seller":"продавец/получатель","tax_id":"налоговый номер продавца или пустая строка","doc_ref":"номер документа или пустая строка","doc_date":"YYYY-MM-DD или null","subtotal":число или null,"vat":число или null,"total":число или null,"currency":"THB/KZT/RUB/USD или пустая строка","payment_method":"cash|card|transfer|qr|unknown","confidence":число от 0 до 1,"note":"короткое пояснение, если что-то неразборчиво"}

Сумма total — итоговая к оплате. Не выдумывай: чего не видно на фото — null или пустая строка, и снижай confidence.
Если на фото нет финансового документа (товар, ценник, люди, скриншот без сумм) — верни только: NOT_A_DOCUMENT"""

NUMERIC_FIELDS = ('subtotal', 'vat', 'total')
TEXT_FIELDS = ('seller', 'tax_id', 'doc_ref', 'currency', 'note')


def _to_number(value: Any) -> float | None:
    if value is None or value == '':
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r'[^\d.,-]', '', str(value))
    if not cleaned:
        return None
    if ',' in cleaned and '.' in cleaned:
        cleaned = cleaned.replace(',', '')
    elif ',' in cleaned:
        cleaned = cleaned.replace(',', '.') if re.search(r',\d{1,2}$', cleaned) else cleaned.replace(',', '')
    try:
        return float(cleaned)
    except ValueError:
        return None


def normalize_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce the model's JSON into the fixed shape the store and checks rely on."""
    doc_type = data.get('doc_type') if data.get('doc_type') in DOC_TYPES else 'other'
    doc_date = None
    raw_date = str(data.get('doc_date') or '')[:10]
    try:
        doc_date = dt.date.fromisoformat(raw_date).isoformat()
    except ValueError:
        doc_date = None
    try:
        confidence = max(0.0, min(1.0, float(data.get('confidence'))))
    except (TypeError, ValueError):
        confidence = 0.0
    fields: dict[str, Any] = {
        'doc_type': doc_type,
        'doc_date': doc_date,
        'payment_method': data.get('payment_method') if data.get('payment_method') in PAYMENT_METHODS else 'unknown',
        'confidence': confidence,
    }
    for key in NUMERIC_FIELDS:
        fields[key] = _to_number(data.get(key))
    for key in TEXT_FIELDS:
        fields[key] = str(data.get(key) or '').strip()[:300]
    return fields


def parse_document_result(result: str | None) -> dict[str, Any] | None:
    if not result or 'NOT_A_DOCUMENT' in result:
        return None
    match = re.search(r'\{.*\}', result, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except ValueError:
        return None
    return normalize_fields(data) if isinstance(data, dict) else None


def analyze_document_image(image_b64: str, mime: str = 'image/jpeg', context: str = '') -> dict[str, Any] | None:
    import app
    from notimate.processing.ai import document_input_part
    prompt = DOCUMENT_PROMPT + (f'\n\nКонтекст клиента: {context}' if context else '')
    content = [
        {'type': 'input_text', 'text': 'Проанализируй документ.'},
        document_input_part(image_b64, mime),
    ]
    return parse_document_result(app.ask_openai(prompt, [{'role': 'user', 'content': content}], 1200))


def document_is_confident(fields: dict[str, Any], min_confidence: float = 0.7) -> bool:
    if fields.get('doc_type') == 'other' or fields.get('total') is None or not fields.get('doc_date'):
        return False
    if fields.get('confidence', 0) < min_confidence:
        return False
    subtotal, vat, total = fields.get('subtotal'), fields.get('vat'), fields['total']
    if subtotal is not None and vat is not None:
        if abs(subtotal + vat - total) > max(1.0, total * 0.005):
            return False
    return total >= 0
