"""Document markers in expense messages — the answer to «Не хватает» being too long.

Without a signal, every expense typed as text («молоко 200») looks like an «expense without a
document» and floods the owner's list. Instead the sender says what the document situation is,
in the same message, in any of RU / EN / TH:

* «чек», «накладная», «счёт», «invoice», «receipt», «ใบเสร็จ» … → a document **is expected**
  (photo to follow): if none arrives, it shows up in «Не хватает»;
* «без чека», «нет чека», «no receipt», «ไม่มีใบเสร็จ» … → **confirmed: no document** (market,
  cash purchase): recorded as such and never reported as missing;
* neither → not checked (counted in the summary line so nothing is hidden).

Negative phrases are tested first because they contain the positive word («без чека»).
A tenant may switch to the strict behaviour (every expense expects a document) with
``modules.accountant.rules.expense_doc_policy = "all"``.
"""

from __future__ import annotations

import re

EXPECTED = 'expected'
NONE = 'none'

_NEGATIVE = re.compile(
    r'без\s+(?:чек|накладн|документ|счёт|счет|квитанц)|нет\s+(?:чека|накладной|документа|счёта|счета)|не\s+дали\s+чек'
    r'|no\s+(?:receipt|invoice|bill|document)|without\s+(?:a\s+)?(?:receipt|invoice)|ไม่มี(?:ใบเสร็จ|ใบกำกับ|บิล)',
    re.IGNORECASE,
)
_POSITIVE = re.compile(
    r'чек|накладн|счёт|счет[- ]?фактур|инвойс|фактур|квитанц|\breceipt\b|\binvoice\b|\bbill\b|tax\s+invoice|ใบเสร็จ|ใบกำกับ|ใบส่งของ',
    re.IGNORECASE,
)


def document_marker(text: str | None) -> str | None:
    """``'none'`` (no document, confirmed), ``'expected'`` (document to follow) or ``None`` (no signal)."""
    if not text:
        return None
    if _NEGATIVE.search(text):
        return NONE
    if _POSITIVE.search(text):
        return EXPECTED
    return None
