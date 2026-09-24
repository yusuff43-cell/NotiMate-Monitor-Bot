#!/usr/bin/env python3
"""Create a new client's Google Sheet with the standard NotiMate tabs and share it with the owner.

The bot's service account creates the spreadsheet (so it can write to it) and gives the
owner edit access by e-mail — the owner then watches it from a phone or a computer like any
Google Sheet. Prints the spreadsheet id: put it in the tenant's ``sheet_id`` (tenant config
JSON → ``deploy/apply_tenant_config.py``).

    docker compose -f compose.vps.yml exec -T worker python deploy/create_client_sheet.py \
        --title "NotiMate — Ержан" --currency KZT --share-with owner@example.com

Tabs are the same the LINE bot writes; the amount columns carry the client's currency.
Nothing is deleted or overwritten; ``--dry-run`` only prints the plan.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import gspread
from google.oauth2.service_account import Credentials

EVENT_ID_HEADER = 'NotiMate Event ID'


def tabs(currency: str) -> dict[str, list[str]]:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--title', help='Title for a NEW spreadsheet (needs the Google Drive API enabled for the project)')
    parser.add_argument('--sheet-id', help='Use an existing spreadsheet the owner created and shared with the service account (Editor) instead of creating one')
    parser.add_argument('--currency', default='THB')
    parser.add_argument('--share-with', action='append', default=[], help='Owner e-mail (repeatable); gets edit access')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    if not (args.title or args.sheet_id):
        parser.error('give --title (create) or --sheet-id (existing)')
    plan = tabs(args.currency)
    print(f"Spreadsheet «{args.title or args.sheet_id}»: tabs = {', '.join(plan)}; share with {len(args.share_with)} address(es)")
    if args.dry_run:
        print('dry run: nothing created')
        return 0
    creds = Credentials.from_service_account_info(
        json.loads(os.environ['GOOGLE_CREDENTIALS']),
        scopes=['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive'],
    )
    gc = gspread.authorize(creds)
    if args.sheet_id:
        sh = gc.open_by_key(args.sheet_id)
        existing = {ws.title for ws in sh.worksheets()}
    else:
        sh = gc.create(args.title)
        existing = set()
    first = not args.sheet_id
    for name, headers in plan.items():
        if name in existing:
            continue  # never touch a tab that already exists
        if first:
            ws = sh.sheet1
            ws.update_title(name)
            first = False
        else:
            ws = sh.add_worksheet(title=name, rows=1000, cols=len(headers) + 1)
        ws.append_row(headers + ([EVENT_ID_HEADER] if name not in ('Не хватает',) else []))
        ws.freeze(rows=1)
    if args.sheet_id and 'Sheet1' in existing and len(existing) == 1:
        pass  # an untouched default tab is left for the owner to delete
    for email in ([] if args.sheet_id else args.share_with):
        sh.share(email, perm_type='user', role='writer', notify=True)
    print(f'created: sheet_id={sh.id}')
    print(f'url: https://docs.google.com/spreadsheets/d/{sh.id}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
