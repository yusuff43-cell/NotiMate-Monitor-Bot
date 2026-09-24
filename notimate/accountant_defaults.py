"""Country → default currency label. Kept tiny and separate so any module can import it."""

CURRENCIES = {'TH': 'THB', 'KZ': 'KZT', 'RU': 'RUB'}


def currency_for(country: str | None) -> str:
    return CURRENCIES.get((country or '').upper(), 'THB')
