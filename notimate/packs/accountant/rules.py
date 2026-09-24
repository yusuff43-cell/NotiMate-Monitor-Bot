"""Country rule sets for document completeness checks (docs/19: «правила проверок хранятся
в настройке пакета страны, а не в коде»).

IMPORTANT: these are conservative defaults, not legal advice. docs/19 requires each
country's requirements (originals, retention, document kinds) to be confirmed with a
practising accountant before launch; a tenant overrides any value through
``tenants.modules['accountant']['rules']`` without a code change. The module never decides
whether an expense is deductible — it only shows which document looks missing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

DOC_TYPES = {
    'tax_invoice': 'Полный tax invoice / счёт-фактура',
    'receipt_simplified': 'Упрощённый чек',
    'supplier_invoice': 'Счёт поставщика',
    'bank_slip': 'Банковский слип / перевод',
    'delivery_note': 'Накладная / АВР',
    'other': 'Другое',
}

PAYMENT_METHODS = ('cash', 'card', 'transfer', 'qr', 'unknown')

COUNTRY_RULES: dict[str, dict[str, Any]] = {
    'TH': {
        'currency': 'THB',
        # Thai input VAT normally needs a full tax invoice in the buyer's name; an
        # abbreviated receipt is flagged when the tenant is VAT-registered.
        'vat_registered': True,
        'full_invoice_min_total': 0,
        'formal_doc_min_total': None,
        'transfer_needs_invoice': True,
        'amount_tolerance': 1.0,
        'date_window_days': 3,
    },
    'KZ': {
        'currency': 'KZT',
        'vat_registered': False,
        'full_invoice_min_total': None,
        # A cash receipt alone is usually not enough for larger supplier purchases; the
        # threshold is left unset until confirmed with the accountant.
        'formal_doc_min_total': None,
        'transfer_needs_invoice': True,
        'amount_tolerance': 1.0,
        'date_window_days': 3,
    },
}

FORMAL_DOC_TYPES = ('tax_invoice', 'supplier_invoice', 'delivery_note')


def rules_for(country: str | None, tenant_modules: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Country defaults overlaid with the tenant's own ``modules.accountant.rules``."""
    rules = dict(COUNTRY_RULES.get((country or '').upper(), COUNTRY_RULES['TH']))
    accountant = (tenant_modules or {}).get('accountant') if isinstance(tenant_modules, Mapping) else None
    overrides = accountant.get('rules') if isinstance(accountant, Mapping) else None
    if isinstance(overrides, Mapping):
        rules.update({key: value for key, value in overrides.items() if key in rules})
    return rules
