"""Expense categories («статьи расходов») for the dashboard.

Categories are derived at read time from the supplier and description text already stored
in ``operations`` — no schema change, retroactive for every past row, and improvable
without touching data. Matching is keyword based (RU / EN / TH / KK fragments), first
matching category wins; a tenant can put its own rules first with
``tenants.modules.expense_categories = {"Категория": ["ключ", ...]}``. Anything unmatched
is «Прочее», so a number is never lost, only less precisely labelled.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

OTHER = 'Прочее'
SALARY = 'Зарплаты'

RULES: list[tuple[str, tuple[str, ...]]] = [
    ('Аренда', ('аренд', 'rent', 'เช่า')),
    ('Коммунальные и связь', ('коммунал', 'электр', 'electric', 'интернет', 'internet', 'газ ', 'gas bill', 'water bill', 'связь', 'sim', 'ค่าไฟ', 'ค่าน้ำ')),
    ('Налоги и сборы', ('налог', 'tax', 'штраф', 'fine', 'лиценз', 'licen', 'сбор', 'ภาษี')),
    ('Транспорт и доставка', ('такси', 'taxi', 'достав', 'deliver', 'grab', 'bolt', 'yandex', 'бензин', 'fuel', 'petrol', 'логист', 'перевозк')),
    ('Ремонт и оборудование', ('ремонт', 'repair', 'оборудов', 'запчаст', 'мастер', 'инструмент', 'плитк', 'сантехн', 'equipment', 'ซ่อม')),
    ('Реклама и маркетинг', ('реклам', 'advert', 'marketing', 'маркетинг', 'instagram', 'таргет', 'флаер', 'баннер', 'печать', 'ads')),
    ('Упаковка', ('упаков', 'стакан', 'крышк', 'cup', 'lid ', 'packag', 'коробк', 'container', 'контейнер', 'пакет', 'bag', 'straw', 'трубочк')),
    ('Хозтовары и уборка', ('хозтовар', 'чистящ', 'моющ', 'салфет', 'перчат', 'губк', 'мусор', 'clean', 'soap', 'мыло', 'tissue', 'chemical', 'дезинф')),
    ('Напитки', ('кофе', 'coffee', 'чай', ' tea', 'вода', 'water', 'сок', 'juice', 'cola', 'кола', 'напит', 'drink', 'beans', 'зерн', 'сироп', 'syrup', 'กาแฟ')),
    ('Продукты и сырьё', (
        'makro', 'макро', 'lotus', 'лотус', 'big c', 'tesco', '7-eleven', '7eleven', 'рынок', 'market', 'базар', 'магнум', 'small',
        'мяс', 'молок', 'овощ', 'фрукт', 'сыр', 'яйц', 'мук', 'сахар', 'круп', 'хлеб', 'рыб', 'куриц', 'масло', 'сливк', 'творог', 'mango', 'avocado',
        'meat', 'milk', 'egg', 'fish', 'chicken', 'vegetable', 'fruit', 'cheese', 'flour', 'sugar', 'butter', 'cream', 'rice', 'продукт', 'сырь',
        'ตลาด', 'นม', 'ไข่', 'หมู', 'ไก่', 'ice', 'лёд', 'лед',
    )),
]


def categorize(counterparty: str | None, description: str | None, operation_type: str = 'expense', overrides: Mapping[str, Any] | None = None) -> str:
    if operation_type == 'salary':
        return SALARY
    text = f" {(counterparty or '')} {(description or '')} ".lower()
    if isinstance(overrides, Mapping):
        for category, keywords in overrides.items():
            if isinstance(keywords, (list, tuple)) and any(str(k).lower() in text for k in keywords if str(k).strip()):
                return str(category)
    for category, keywords in RULES:
        if any(keyword in text for keyword in keywords):
            return category
    return OTHER
