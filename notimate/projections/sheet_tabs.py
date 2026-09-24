"""The standard tab layout of a NotiMate client sheet (same headers the bot writes)."""

from __future__ import annotations

import re

EVENT_ID_HEADER = 'NotiMate Event ID'


def tab_plan(currency: str) -> dict[str, list[str]]:
    return {
        'Закупки': ['Дата', 'Продукт', 'Количество'],
        'Остатки': ['Дата', 'Категория', 'Продукт', 'Холодильник', 'Морозилка', 'Примечание'],
        'Расходы': ['Дата', 'Тип', 'Поставщик/Магазин', 'Позиция', f'Сумма ({currency})', 'Примечание'],
        'Выручка': ['Дата', 'Смена', 'Gross Sales', 'Наличные', 'Карта', 'QR', 'Примечание'],
        'Зарплаты': ['Дата', 'Получатель', f'Сумма ({currency})', 'Примечание'],
        'Проблемы': ['Дата', 'Сообщение', 'Перевод и совет'],
        'Напоминания': ['Название', 'Дата окончания', 'Дата добавления', 'Примечание'],
        'Цены': ['Дата', 'Поставщик', 'Позиция', f'Цена ({currency})'],
        'Отчёты точек': ['Дата', 'Точка', 'Выручка', 'Наличные', 'Безнал', 'Внешние выплаты', 'Остаток наличных', 'Комментарий', 'Сотрудник'],
        'Бухгалтерия': ['№', 'Дата документа', 'Тип', 'Продавец', 'Налоговый №', '№ документа', 'Сумма без НДС', 'НДС', 'Итого', 'Валюта', 'Оплата'],
        'Не хватает': ['Важность', 'Что не хватает / вопрос'],
    }


def parse_sheet_id(text: str) -> str | None:
    """Spreadsheet id from a Google Sheets URL, or the bare id itself."""
    match = re.search(r'/spreadsheets/d/([A-Za-z0-9_-]{20,})', text)
    if match:
        return match.group(1)
    bare = text.strip()
    return bare if re.fullmatch(r'[A-Za-z0-9_-]{30,}', bare) else None


def init_tabs(gc, sheet_id: str, currency: str) -> list[str]:
    """Create the standard tabs that are missing (never touches an existing tab); returns the created names."""
    sh = gc.open_by_key(sheet_id)
    existing = {ws.title for ws in sh.worksheets()}
    created = []
    for name, headers in tab_plan(currency).items():
        if name in existing:
            continue
        ws = sh.add_worksheet(title=name, rows=1000, cols=len(headers) + 1)
        ws.append_row(headers + ([EVENT_ID_HEADER] if name != 'Не хватает' else []))
        ws.freeze(rows=1)
        created.append(name)
    return created
