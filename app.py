import os
import json
import re
import time
import base64
import datetime
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    MessagingApiBlob,
    PushMessageRequest,
    TextMessage,
)
from openai import APIStatusError, OpenAI, RateLimitError
import gspread
from google.oauth2.service_account import Credentials
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

from core import bangkok_date, bangkok_now, client_prompt_context, days_until, validate_clients
from event_store import PostgresEventStore
from logging_utils import get_logger

app = Flask(__name__)
logger = get_logger()

# ── Глобальные сервисы ──────────────────────────────────────────
OPENAI_MODEL = os.environ.get('OPENAI_MODEL', 'gpt-5.6-luna')
openai_client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])

if os.environ.get('CLIENTS_JSON'):
    CLIENTS = json.loads(os.environ['CLIENTS_JSON'])
else:
    with open('clients.json', 'r', encoding='utf-8') as f:
        CLIENTS = json.load(f)

CLIENTS = validate_clients(CLIENTS)

DATABASE_URL = os.environ.get('DATABASE_URL', '')
event_store = PostgresEventStore(DATABASE_URL) if DATABASE_URL else None
DB_ENABLED = False
if event_store:
    try:
        event_store.initialize()
        DB_ENABLED = True
    except Exception as exc:
        logger.error('database_init_failed', extra={'error_type': type(exc).__name__})

SHEETS_ENABLED = False
gc = None
try:
    creds_json = json.loads(os.environ['GOOGLE_CREDENTIALS'])
    scopes = ['https://www.googleapis.com/auth/spreadsheets']
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    gc = gspread.authorize(creds)
    SHEETS_ENABLED = True
except Exception as exc:
    logger.warning('sheets_init_skipped', extra={'error_type': type(exc).__name__})

_line_api_cache = {}

def get_line_clients(token: str):
    if token not in _line_api_cache:
        api_client = ApiClient(Configuration(access_token=token))
        _line_api_cache[token] = (
            MessagingApi(api_client),
            MessagingApiBlob(api_client),
            api_client,
        )
    return _line_api_cache[token]


def get_line_api(token: str) -> MessagingApi:
    return get_line_clients(token)[0]


def get_line_blob_api(token: str) -> MessagingApiBlob:
    return get_line_clients(token)[1]

def find_client(destination: str):
    return CLIENTS.get(destination)


def openai_usage_values(response):
    """Read token counters defensively across compatible Responses SDK versions."""
    usage = getattr(response, 'usage', None)
    input_tokens = int(getattr(usage, 'input_tokens', 0) or 0)
    output_tokens = int(getattr(usage, 'output_tokens', 0) or 0)
    output_details = getattr(usage, 'output_tokens_details', None)
    reasoning_tokens = int(getattr(output_details, 'reasoning_tokens', 0) or 0)
    return input_tokens, output_tokens, reasoning_tokens


def ask_openai(instructions, input_data, max_output_tokens):
    """Call OpenAI with bounded retries and no response storage."""
    for attempt in range(3):
        try:
            response = openai_client.responses.create(
                model=OPENAI_MODEL,
                instructions=instructions,
                input=input_data,
                max_output_tokens=max_output_tokens,
                reasoning={"effort": "none"},
                text={"verbosity": "low"},
                store=False,
            )
            result = (response.output_text or '').strip()
            if not result:
                raise RuntimeError('OpenAI returned an empty response')
            input_tokens, output_tokens, reasoning_tokens = openai_usage_values(response)
            logger.info(
                'openai_request_completed',
                extra={
                    'model': OPENAI_MODEL,
                    'input_tokens': input_tokens,
                    'output_tokens': output_tokens,
                    'reasoning_tokens': reasoning_tokens,
                },
            )
            if event_store and DB_ENABLED:
                try:
                    event_store.record_openai_usage(
                        OPENAI_MODEL, input_tokens, output_tokens, reasoning_tokens
                    )
                except Exception as exc:
                    # Usage accounting must never prevent the owner from receiving a result.
                    logger.warning('openai_usage_recording_failed', extra={'error_type': type(exc).__name__})
            return result
        except (RateLimitError, APIStatusError) as exc:
            status_code = getattr(exc, 'status_code', None)
            retryable = isinstance(exc, RateLimitError) or status_code in {408, 409, 429} or (status_code and status_code >= 500)
            if retryable and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise

# ── Google Sheets helpers ────────────────────────────────────────
EVENT_ID_HEADER = 'NotiMate Event ID'


def get_or_create_sheet(sh, name, headers):
    try:
        ws = sh.worksheet(name)
    except:
        ws = sh.add_worksheet(title=name, rows=1000, cols=len(headers) + 1)
        ws.append_row(headers + [EVENT_ID_HEADER])
    ensure_event_id_column(ws)
    return ws


def ensure_event_id_column(ws):
    """Return the one-based technical event-ID column, adding it if necessary.

    The ID is deliberately stored in the spreadsheet: a worker can be killed after
    Google accepted an append but before it acknowledges the event in Postgres.
    On retry, this durable marker makes the projection safe to repeat.
    """
    headers = ws.row_values(1)
    if EVENT_ID_HEADER not in headers:
        column = len(headers) + 1
        # Existing client sheets may have an exact-width grid.  Extending the
        # grid before writing the technical column keeps idempotency compatible
        # with those sheets instead of failing with Google API 400 grid limits.
        current_columns = getattr(ws, 'col_count', None)
        if current_columns is not None and current_columns < column:
            ws.add_cols(column - current_columns)
        ws.update_cell(1, column, EVENT_ID_HEADER)
        return column
    return headers.index(EVENT_ID_HEADER) + 1


def append_rows_once(ws, rows, event_id, effect):
    """Append the rows not yet projected for one LINE event and verify the write."""
    if not event_id:
        raise ValueError('Cannot write to Google Sheets without webhookEventId')
    if not rows:
        return 0
    event_column = ensure_event_id_column(ws)
    keys = [f'{event_id}:{effect}:{index}' for index in range(len(rows))]
    existing_keys = set(ws.col_values(event_column)[1:])
    missing = []
    for row, key in zip(rows, keys):
        if key in existing_keys:
            continue
        padded = list(row[:event_column - 1])
        padded.extend([''] * (event_column - 1 - len(padded)))
        padded.append(key)
        missing.append(padded)
    if not missing:
        return 0
    ws.append_rows(missing, value_input_option='USER_ENTERED')
    written_keys = set(ws.col_values(event_column)[1:])
    expected = {key for key in keys if key not in existing_keys}
    if not expected.issubset(written_keys):
        raise RuntimeError('Google Sheets did not confirm all appended event rows')
    return len(missing)

def get_last_date(ws):
    try:
        col = ws.col_values(1)
        for val in reversed(col):
            if val and val != 'Дата':
                return val
    except:
        pass
    return None

def save_остатки(sheet_id, items, date_str, event_id):
    if not gc:
        raise RuntimeError('Google Sheets is not ready')
    sh = gc.open_by_key(sheet_id)
    headers = ['Дата', 'Категория', 'Продукт', 'Холодильник', 'Морозилка', 'Примечание']
    ws = get_or_create_sheet(sh, 'Остатки', headers)
    last_date = get_last_date(ws)
    last_category = None
    rows = []
    for i, item in enumerate(items):
        date_cell = date_str if (i == 0 and last_date != date_str) else ''
        category = item.get('category', '')
        category_cell = category if category != last_category else ''
        if category:
            last_category = category
        rows.append([date_cell, category_cell, item.get('product',''), item.get('fridge',''), item.get('freezer',''), item.get('note','')])
    return append_rows_once(ws, rows, event_id, 'stock')

def save_закупки(sheet_id, items, date_str, event_id):
    if not gc:
        raise RuntimeError('Google Sheets is not ready')
    sh = gc.open_by_key(sheet_id)
    headers = ['Дата', 'Продукт', 'Количество']
    ws = get_or_create_sheet(sh, 'Закупки', headers)
    last_date = get_last_date(ws)
    rows = []
    for i, item in enumerate(items):
        date_cell = date_str if (i == 0 and last_date != date_str) else ''
        rows.append([date_cell, item.get('product',''), item.get('quantity','')])
    return append_rows_once(ws, rows, event_id, 'purchase')

def save_расходы(sheet_id, items, date_str, supplier, note='', event_id=None, effect='expense'):
    if not gc:
        raise RuntimeError('Google Sheets is not ready')
    sh = gc.open_by_key(sheet_id)
    headers = ['Дата', 'Тип', 'Поставщик/Магазин', 'Позиция', 'Сумма (THB)', 'Примечание']
    ws = get_or_create_sheet(sh, 'Расходы', headers)
    rows = []
    for item in items:
        clean_amount = str(item.get('amount','') or '').replace('฿','').replace('B','').replace(',','').strip()
        rows.append([date_str, item.get('type','Закупка'), item.get('supplier', supplier), item.get('description',''), clean_amount, item.get('note', note)])
    return append_rows_once(ws, rows, event_id, effect)

def save_выручка(sheet_id, data, date_str, note='', event_id=None):
    if not gc:
        raise RuntimeError('Google Sheets is not ready')
    sh = gc.open_by_key(sheet_id)
    headers = ['Дата', 'Смена', 'Gross Sales', 'Наличные', 'Карта', 'QR', 'Примечание']
    ws = get_or_create_sheet(sh, 'Выручка', headers)
    return append_rows_once(ws, [[date_str, data.get('shift',''), data.get('gross_sales',''), data.get('cash',''), data.get('card',''), data.get('qr',''), note]], event_id, 'shift')

def save_проблемы(sheet_id, text, result, date_str, event_id):
    if not gc:
        raise RuntimeError('Google Sheets is not ready')
    sh = gc.open_by_key(sheet_id)
    headers = ['Дата', 'Сообщение', 'Перевод и совет']
    ws = get_or_create_sheet(sh, 'Проблемы', headers)
    return append_rows_once(ws, [[date_str, text, result]], event_id, 'problem')


def save_одиночный_остаток(sheet_id, product, amount, date_str, event_id):
    sh = gc.open_by_key(sheet_id)
    ws = get_or_create_sheet(sh, 'Остатки', ['Дата','Категория','Продукт','Холодильник','Морозилка','Примечание'])
    return append_rows_once(ws, [[date_str, '', product, amount, '', '']], event_id, 'single-stock')


def save_зарплаты(sheet_id, items, date_str, event_id):
    sh = gc.open_by_key(sheet_id)
    ws = get_or_create_sheet(sh, 'Зарплаты', ['Дата','Получатель','Сумма (THB)','Примечание'])
    rows = [[date_str, item.get('recipient',''), item.get('amount',''), item.get('note','')] for item in items]
    return append_rows_once(ws, rows, event_id, 'salary')


def save_напоминание(sheet_id, data, date_str, event_id):
    sh = gc.open_by_key(sheet_id)
    ws = get_or_create_sheet(sh, 'Напоминания', ['Название', 'Дата окончания', 'Дата добавления', 'Примечание'])
    row = [data.get('title',''), data.get('expiry_date',''), date_str, data.get('note','')]
    return append_rows_once(ws, [row], event_id, 'reminder')

def notify_owner(client_cfg, msg):
    try:
        api = get_line_api(client_cfg['channel_access_token'])
        recipients = [client_cfg['owner_line_id']]
        if client_cfg.get('owner_line_id_2'):
            recipients.append(client_cfg['owner_line_id_2'])
        for recipient in recipients:
            api.push_message(PushMessageRequest(
                to=recipient,
                messages=[TextMessage(text=msg)],
            ))
        return True
    except Exception as exc:
        logger.error('owner_notification_failed', extra={'error_type': type(exc).__name__})
        return False

# ── Анализ фото ──────────────────────────────────────────────────
def check_price_drift(sheet_id, items, supplier, client_cfg, event_id):
    """Проверяет дрейф цен и уведомляет если цена выросла >10%"""
    if not gc:
        return
    try:
        sh = gc.open_by_key(sheet_id)
        headers = ['Дата', 'Поставщик', 'Позиция', 'Цена (THB)']
        ws = get_or_create_sheet(sh, 'Цены', headers)
        rows = ws.get_all_records()
        alerts = []
        price_rows = []
        date_today = bangkok_date()
        for item in items:
            name = item.get('description', '').strip()
            try:
                price = float(str(item.get('unit_price', item.get('amount', 0)) or 0).replace('฿','').replace(',','').strip() or 0)
            except:
                price = 0
            if not name or price <= 0:
                continue
            # Ищем последнюю цену этой позиции
            prev_price = None
            for row in reversed(rows):
                if row.get('Позиция','').lower() == name.lower():
                    try:
                        prev_price = float(str(row.get('Цена (THB)', 0) or 0).replace('฿','').replace(',','').strip() or 0)
                    except:
                        prev_price = None
                    break
            price_rows.append([date_today, supplier, name, price])
            # Проверяем дрейф
            if prev_price and prev_price > 0 and price > 0:
                drift = (price - prev_price) / prev_price * 100
                if drift >= 10:
                    alerts.append(f"- {name}: {prev_price:.0f} → {price:.0f} THB (+{drift:.0f}%)")
        created = append_rows_once(ws, price_rows, event_id, 'price')
        if alerts and created:
            msg = f"⚠️ ДРЕЙФ ЦЕН от {supplier}:\n"
            msg += "\n".join(alerts)
            msg += "\n\n💡 Проверьте накладную — поставщик поднял цены."
            notify_owner(client_cfg, msg)
    except Exception as exc:
        logger.error('price_drift_check_failed', extra={'event_id': event_id, 'error_type': type(exc).__name__})


def analyze_image(image_data, client_cfg):
    business_context = client_prompt_context(client_cfg)
    return ask_openai(
        f"""Ты анализируешь фото документов для бизнеса. Входные документы могут быть на русском, тайском или английском языке. Распознавай все три языка, но все названия, пояснения, переводы и советы возвращай только на русском языке.
Контекст клиента: {business_context}

Если это SHIFT REPORT (содержит Shift number, Gross sales, Cash drawer):
Верни ТОЛЬКО JSON: {{"doc_type":"shift","shift":"номер смены","gross_sales":число,"cash":число,"card":число,"qr":число,"difference":число,"note":""}}

Если это НАКЛАДНАЯ от поставщика:
Верни ТОЛЬКО JSON: {{"doc_type":"invoice","supplier":"поставщик","items":[{{"description":"позиция на русском","unit_price":"цена за единицу (Unit Price)","amount":"общая сумма по позиции (Amount)"}}],"total":"итого","note":""}}

Если это ЧЕК или фото покупки:
Верни ТОЛЬКО JSON: {{"doc_type":"expense","supplier":"магазин","items":[{{"description":"что купили на русском","amount":"сумма"}}],"total":"итого","note":""}}

Если это БАНКОВСКИЙ ПЕРЕВОД сотруднику (Transfer Completed, KBIZ, SCB):
Верни ТОЛЬКО JSON: {{"doc_type":"salary","recipient":"имя получателя","amount":0,"note":""}}

Если это ДОКУМЕНТ С ДАТОЙ ОКОНЧАНИЯ (лицензия, разрешение, аренда, страховка, сертификат):
Верни ТОЛЬКО JSON: {{"doc_type":"reminder","title":"название документа на русском","expiry_date":"YYYY-MM-DD","note":""}}

Если это ОБЪЯВЛЕНИЕ или УВЕДОМЛЕНИЕ:
Верни ТОЛЬКО JSON: {{"doc_type":"notice","title":"заголовок на русском","content":"перевод на русский","note":""}}

Если это СКРИНШОТ ИСТОРИИ ТРАНЗАКЦИЙ из банковского приложения (Transaction history, Payment, Transfer, Top up):
Верни ТОЛЬКО JSON: {{"doc_type":"bank_history","items":[{{"type":"expense","recipient":"получатель","amount":0,"note":""}}]}}
Правила:
- Payment/Scan to pay → type=expense
- Transfer PromptPay к физлицу (имя) → type=salary
- Top up PromptPay Wallet → type=expense
- Игнорируй строки без суммы

Скриншоты магазинов, ценники, фото продуктов без чека — верни: NOT_FINANCE
Если не финансовый документ — верни: NOT_FINANCE""",
        [{"role": "user", "content": [
            {"type": "input_text", "text": "Проанализируй документ."},
            {"type": "input_image", "image_url": f"data:image/jpeg;base64,{image_data}", "detail": "high"},
        ]}],
        2000,
    )

# ── Анализ текста ────────────────────────────────────────────────
def analyze_text(text, client_cfg):
    business_context = client_prompt_context(client_cfg)
    return ask_openai(
        f"""КРИТИЧЕСКИ ВАЖНО: Только анализируй сообщения по правилам. Если не подходит — верни ТОЛЬКО: IGNORE
Входное сообщение может быть на русском, тайском или английском языке. Понимай все три языка. Названия товаров, описание проблемы, перевод, рекомендации и весь текст для владельца возвращай только на русском языке. JSON-ключи сохраняй точно по схеме.
КОНТЕКСТ КЛИЕНТА: {business_context}

СЛОВАРЬ: Clear/Clear croissant=Масляный круассан, Chocolate=Шоколадный круассан, Almond=Миндальный круассан, Ham Cheese=Круассан с ветчиной и сыром, Cheesecake=Чизкейк, Biscoff cheesecake=Бискофф чизкейк, Cheese pancakes=Сырники, Mango cheese pancakes=Манговые сырники, Cucumber cheese pancakes=Огуречные сырники, Crepes=Шпинатные блинчики, Pancakes=Панкейки, Crepes burger=Блины для бургера, Pannacotta=Панна-котта, Chocolate mousse=Шоколадный мусс, Salted Caramel=Солёная карамель, Bounty=Баунти, Halva=Халва, Marzipan=Марципан, Brownie=Брауни, Banana bread=Банановый хлеб, Muffin=Маффин, Snickers=Сникерс, Napoleons=Наполеон, Sourdough=Хлеб на закваске (для брускет), Banana=Банан (не банановый хлеб), Coconut velvet=Кокосовое молоко велюр, Coconut milk velvet=Кокосовое молоко велюр, Dragon fruit=Драгон фрут, Salmon=Лосось, Yogurt=Йогурт, Açaí=Асаи

ТИПЫ СООБЩЕНИЙ:

1. ЗАКУПКИ - сообщения со списком продуктов для заказа. Триггеры в начале: "we need", "for tomorrow", "need", "order". ИЛИ просто список продуктов с количествами без цен (каждая строка = продукт + количество).
Если есть сумма (฿, =число฿) — это РАСХОД (тип 4), не закупка
Верни ТОЛЬКО JSON: {{"type":"purchase","items":[{{"product":"название на русском","quantity":"только цифра без единиц измерения"}}]}}

2. ОСТАТКИ - начинаются с "Update"
Верни ТОЛЬКО JSON: {{"type":"stock","items":[{{"category":"Круассаны/Десерты/Блины и сырники/Макаруны/Начинки/Другое","product":"название на русском","fridge":"","freezer":"","note":""}}]}}
Правила note: "Out of stock" если всё 0, "Low stock" если 1-2 шт, "Exp today" если помечено

3. ОСТАТОК ОДНОЙ ПОЗИЦИИ - "товар have/has количество" или "товар количество г/pcs". БЕЗ цены (฿). Если есть ฿ — это РАСХОД (тип 4)
Верни ТОЛЬКО JSON: {{"type":"single_stock","product":"название на русском","amount":"количество"}}

4. РАСХОД - покупка с подтверждением (bought, paid, total, ฿, -)
Триггеры: "bought","paid","TOTAL","total","spent","-число฿ for", минус перед суммой, "=число฿", просто "число฿" или "฿число", формат "N товар =сумма฿" (например: 1 ice =35฿, 2 bag of ice =70฿), формат "N товар TOTAL число" (например: 2 sourdough TOTAL 110)
Верни ТОЛЬКО JSON: {{"type":"text_expense","supplier":"магазин","items":[{{"description":"что купили на русском"}}],"total":"сумма"}}

5. ПРОБЛЕМА - поломки, аварии, инциденты
Верни: ВАЖНО [ПРОБЛЕМА]: [описание]\\n💡 Совет: [совет]

Если не подходит — верни только: IGNORE""",
        text,
        8096,
    )

# ── Утренняя сводка ──────────────────────────────────────────────
_last_report = {}
def weekly_report(client_cfg):
    """Еженедельная аналитика — воскресенье 18:00"""
    if not gc:
        return
    try:
        tz = pytz.timezone('Asia/Bangkok')
        now = datetime.datetime.now(tz)
        week_ago = (now - datetime.timedelta(days=7)).strftime('%Y-%m-%d')
        date_today = now.strftime('%Y-%m-%d')
        sh = gc.open_by_key(client_cfg['sheet_id'])

        # Выручка за неделю
        total_revenue = 0
        try:
            ws_rev = sh.worksheet('Выручка')
            rows_rev = ws_rev.get_all_records()
            week_rev = [r for r in rows_rev if str(r.get('Дата','')).strip()[:10] >= week_ago]
            total_revenue = sum(float(str(r.get('Gross Sales',0) or 0).replace('฿','').replace(',','').strip() or 0) for r in week_rev)
        except: pass

        # Расходы за неделю
        total_expenses = 0
        try:
            ws_exp = sh.worksheet('Расходы')
            rows_exp = ws_exp.get_all_records()
            week_exp = [r for r in rows_exp if str(r.get('Дата','')).strip()[:10] >= week_ago]
            def safe_float(v):
                try: return float(str(v or 0).replace('฿','').replace(',','').replace('B','').strip() or 0)
                except: return 0
            total_expenses = sum(safe_float(r.get('Сумма (THB)',0)) for r in week_exp)
        except: pass

        # Проблемы за неделю
        problems_text = ''
        try:
            ws_prob = sh.worksheet('Проблемы')
            rows_prob = ws_prob.get_all_records()
            week_prob = [r for r in rows_prob if str(r.get('Дата','')).strip()[:10] >= week_ago]
            if week_prob:
                problems_text = '\n'.join([f"- {r.get('Сообщение','')[:50]}" for r in week_prob[-3:]])
        except: pass

        # Критичные остатки
        out_of_stock = []
        try:
            ws_ost = sh.worksheet('Остатки')
            rows_ost = ws_ost.get_all_records()
            last_date = max([r.get('Дата','') for r in rows_ost if r.get('Дата','')], default='')
            if last_date:
                last_rows = [r for r in rows_ost if str(r.get('Дата','')) == last_date or not r.get('Дата','')]
                out_of_stock = [r.get('Продукт','') for r in last_rows if r.get('Примечание','') == 'Out of stock']
        except: pass

        profit = total_revenue - total_expenses
        profit_sign = '+' if profit >= 0 else ''

        report = ask_openai(
            'Составь короткую еженедельную сводку для русскоязычного владельца кофейни. Пиши только на русском, конкретно и кратко, максимум 15 строк.',
            f"""Данные за неделю ({week_ago} — {date_today}):
Выручка: {total_revenue:.0f} THB
Расходы: {total_expenses:.0f} THB
Прибыль: {profit_sign}{profit:.0f} THB
Проблемы: {problems_text or 'не зафиксировано'}
Закончилось: {', '.join(out_of_stock[:5]) or 'всё в норме'}

Формат:
📊 Итоги недели [даты]
💰 Выручка: X THB
💸 Расходы: X THB
📈 Прибыль: X THB
⚠️ Проблемы: список или 'нет'
🔴 Закончилось: список или 'всё ок'
💡 Вывод: 1-2 предложения""",
            800,
        )
        notify_owner(client_cfg, report)
    except Exception as exc:
        logger.error('weekly_report_failed', extra={'error_type': type(exc).__name__})


def _money(value):
    try:
        return float(str(value or 0).replace('฿', '').replace(',', '').replace('B', '').strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _rows_for_date(rows, date_prefix, amount_field):
    return sum(_money(row.get(amount_field)) for row in rows if str(row.get('Дата', '')).startswith(date_prefix))


def _available_business_dates(*row_sets):
    dates = set()
    for rows in row_sets:
        for row in rows:
            candidate = str(row.get('Дата', '')).strip()[:10]
            try:
                datetime.datetime.strptime(candidate, '%Y-%m-%d')
                dates.add(candidate)
            except ValueError:
                continue
    return sorted(dates)


def _dashboard_label(value, limit=22):
    """Keep chart labels scannable without changing source worksheet data."""
    label = ' '.join(str(value or '').split()) or 'Без поставщика'
    return label if len(label) <= limit else f'{label[:limit - 1].rstrip()}…'


def refresh_overview(client_cfg):
    """Refresh the owner dashboard after a confirmed projection."""
    if not gc:
        raise RuntimeError('Google Sheets is not ready')
    tz = pytz.timezone('Asia/Bangkok')
    now = datetime.datetime.now(tz)
    sh = gc.open_by_key(client_cfg['sheet_id'])

    try:
        revenue_rows = sh.worksheet('Выручка').get_all_records()
    except Exception:
        revenue_rows = []
    try:
        expense_rows = sh.worksheet('Расходы').get_all_records()
    except Exception:
        expense_rows = []
    available_dates = _available_business_dates(revenue_rows, expense_rows)
    dashboard_date = available_dates[-1] if available_dates else now.strftime('%Y-%m-%d')
    dashboard_day = datetime.datetime.strptime(dashboard_date, '%Y-%m-%d').replace(tzinfo=tz)
    month = dashboard_date[:7]
    revenue_today = _rows_for_date(revenue_rows, dashboard_date, 'Gross Sales')
    expenses_today = _rows_for_date(expense_rows, dashboard_date, 'Сумма (THB)')
    revenue_month = _rows_for_date(revenue_rows, month, 'Gross Sales')
    expenses_month = _rows_for_date(expense_rows, month, 'Сумма (THB)')
    daily = {}
    for offset in range(13, -1, -1):
        day = (dashboard_day - datetime.timedelta(days=offset)).strftime('%Y-%m-%d')
        daily[day] = [
            _rows_for_date(revenue_rows, day, 'Gross Sales'),
            _rows_for_date(expense_rows, day, 'Сумма (THB)'),
        ]
    suppliers = {}
    for row in expense_rows:
        if not str(row.get('Дата', '')).startswith(month):
            continue
        supplier = str(row.get('Поставщик/Магазин', '')).strip() or 'Без поставщика'
        suppliers[supplier] = suppliers.get(supplier, 0.0) + _money(row.get('Сумма (THB)'))
    top_suppliers = sorted(suppliers.items(), key=lambda item: item[1], reverse=True)[:6] or [('Нет данных', 0)]

    critical = []
    try:
        stock_rows = sh.worksheet('Остатки').get_all_records()
        current_date = ''
        dated_rows = []
        for row in stock_rows:
            if str(row.get('Дата', '')).strip():
                current_date = str(row.get('Дата', '')).strip()
            if row.get('Продукт'):
                dated_rows.append((current_date, row))
        latest_date = max((date for date, _ in dated_rows if date), default='')
        critical = [
            row for date, row in dated_rows
            if date == latest_date and row.get('Примечание') in ('Out of stock', 'Low stock', 'Exp today')
        ][:10]
    except Exception:
        pass
    reminders = upcoming_reminders(sh, now, days_limit=14, limit=10)

    values = [['' for _ in range(18)] for _ in range(37)]

    def put(row, column, value):
        values[row - 1][column - 1] = value

    display_date = dashboard_day.strftime('%d.%m.%Y')
    put(1, 1, 'NotiMate')
    put(3, 1, 'Ваш бизнес под контролем')
    put(1, 4, 'Обзор владельца')
    put(3, 4, 'Продажи · Расходы · Остатки · Сроки годности')
    put(1, 11, 'Период')
    put(2, 11, display_date)
    put(1, 14, 'Обновлено (Bangkok)')
    put(2, 14, now.strftime('%d.%m.%Y %H:%M'))
    put(3, 14, f'Последний день данных: {display_date}')
    put(1, 17, 'Данные из Google Sheets')
    put(3, 17, 'Обновляется автоматически')

    cards = [
        (1, f'Выручка · {display_date}', revenue_today, 'Выручка за месяц', revenue_month),
        (5, f'Расходы · {display_date}', expenses_today, 'Расходы за месяц', expenses_month),
        (9, 'Результат дня', revenue_today - expenses_today, 'Результат за месяц', revenue_month - expenses_month),
        (13, 'Критичных позиций', len(critical), 'Сроков ≤ 14 дней', len(reminders)),
    ]
    for column, day_label, day_value, month_label, month_value in cards:
        put(5, column, day_label)
        put(6, column, day_value)
        put(8, column, month_label)
        put(9, column, month_value)

    put(12, 15, 'Товары по срокам годности')
    put(14, 15, f'{len(reminders)}\nтоваров ≤ 14 дней')
    if reminders:
        left, title, expiry = reminders[0]
        put(18, 15, f'Ближайший: {title} · {left} дн.')
    put(19, 15, 'Нет сроков в ближайшие 14 дней' if not reminders else 'Проверьте ближайшие сроки')
    put(21, 15, 'Отлично! Можно не беспокоиться.' if not reminders else 'Откройте вкладку «Напоминания».')
    put(26, 1, 'Критичные остатки')
    put(27, 1, 'Товар')
    put(27, 5, 'Статус')
    put(27, 7, 'Холодильник')
    put(27, 9, 'Морозилка')
    put(26, 10, 'Финансовый итог за месяц')
    put(28, 10, 'Выручка')
    put(29, 10, 'Расходы')
    put(30, 10, 'Результат')
    put(28, 14, revenue_month)
    put(29, 14, expenses_month)
    put(30, 14, revenue_month - expenses_month)
    put(32, 10, 'Убыток за месяц' if revenue_month < expenses_month else 'Результат за месяц')
    put(33, 10, f"Расходы превышают выручку на {abs(revenue_month - expenses_month):,.0f} THB" if revenue_month < expenses_month else 'Выручка превышает расходы.')
    put(26, 15, 'Быстрые действия')
    put(28, 15, 'Добавить покупку — напишите в LINE')
    put(30, 15, 'Проверить остатки')
    put(32, 15, 'Открыть отчёты')
    put(34, 15, 'Настройки и напоминания')
    put(37, 1, 'NotiMate  ·  Данные из Google Sheets  ·  Обновляется автоматически')
    if critical:
        critical_rows = [[row.get('Продукт', ''), row.get('Примечание', ''), row.get('Холодильник', ''), row.get('Морозилка', '')] for row in critical]
    else:
        critical_rows = [['Нет критичных остатков', '', '', '']]
    if reminders:
        reminder_rows = [[title, expiry, left] for left, title, expiry in reminders]
    else:
        reminder_rows = [['Нет сроков в ближайшие 14 дней', '', '']]
    for index, row in enumerate(critical_rows[:7], start=28):
        put(index, 1, row[0])
        put(index, 5, row[1])
        put(index, 7, row[2])
        put(index, 9, row[3])
    trend_rows = [['Дата', 'Выручка (THB)', 'Расходы (THB)']] + [[day, revenue, expense] for day, (revenue, expense) in daily.items()]
    supplier_rows = [['Поставщик', 'Расходы (THB)']] + [[_dashboard_label(supplier), amount] for supplier, amount in top_suppliers]

    try:
        ws = sh.worksheet('Обзор')
    except Exception:
        ws = sh.add_worksheet(title='Обзор', rows=100, cols=12)
    if getattr(ws, 'col_count', 22) < 22:
        ws.add_cols(22 - ws.col_count)
    if hasattr(ws, 'unmerge_cells'):
        try:
            ws.unmerge_cells('A1:R40')
        except Exception:
            pass
    ws.batch_clear(['A1:V45'])
    ws.update(values=values, range_name='A1:R37', value_input_option='USER_ENTERED')
    ws.update(values=trend_rows, range_name=f'T1:V{len(trend_rows)}', value_input_option='USER_ENTERED')
    ws.update(values=supplier_rows, range_name=f'T20:U{19 + len(supplier_rows)}', value_input_option='USER_ENTERED')
    dark_green = {'red': 0.13, 'green': 0.31, 'blue': 0.24}
    light_green = {'red': 0.94, 'green': 0.98, 'blue': 0.95}
    pale_green = {'red': 0.9, 'green': 0.96, 'blue': 0.92}
    pale_red = {'red': 0.99, 'green': 0.91, 'blue': 0.91}
    pale_amber = {'red': 1.0, 'green': 0.96, 'blue': 0.86}
    pale_blue = {'red': 0.91, 'green': 0.95, 'blue': 1.0}
    merged_ranges = [
        'A1:C2', 'D1:I2', 'K1:M2', 'N1:P2', 'Q1:R2',
        'A5:D5', 'A6:D7', 'A8:D8', 'A9:D10',
        'E5:H5', 'E6:H7', 'E8:H8', 'E9:H10',
        'I5:L5', 'I6:L7', 'I8:L8', 'I9:L10',
        'M5:R5', 'M6:R7', 'M8:R8', 'M9:R10',
        'O12:R12', 'O14:R17', 'O18:R18', 'O19:R20', 'O21:R22',
        'A26:I26', 'J26:N26', 'O26:R26', 'J32:N32', 'J33:N34',
        'O28:R28', 'O30:R30', 'O32:R32', 'O34:R34', 'A37:R37',
    ]
    if hasattr(ws, 'merge_cells'):
        for cell_range in merged_ranges:
            try:
                ws.merge_cells(cell_range)
            except Exception:
                pass
    format_requests = []

    def format_sheet(cell_range, style):
        format_requests.append({'range': cell_range, 'format': style})

    format_sheet('A1:R3', {'backgroundColor': light_green, 'verticalAlignment': 'MIDDLE'})
    format_sheet('A1:C2', {'textFormat': {'bold': True, 'fontSize': 16, 'foregroundColor': dark_green}, 'horizontalAlignment': 'CENTER'})
    format_sheet('A3:C3', {'textFormat': {'italic': True, 'fontSize': 9, 'foregroundColor': {'red': 0.2, 'green': 0.45, 'blue': 0.31}}, 'horizontalAlignment': 'CENTER'})
    format_sheet('D1:I2', {'textFormat': {'bold': True, 'fontSize': 16, 'foregroundColor': {'red': 0.05, 'green': 0.12, 'blue': 0.23}}, 'verticalAlignment': 'BOTTOM'})
    format_sheet('D3:I3', {'textFormat': {'fontSize': 10, 'foregroundColor': {'red': 0.33, 'green': 0.39, 'blue': 0.46}}})
    format_sheet('K1:M2', {'backgroundColor': {'red': 1, 'green': 1, 'blue': 1}, 'textFormat': {'bold': True, 'fontSize': 10}, 'horizontalAlignment': 'CENTER', 'verticalAlignment': 'MIDDLE'})
    format_sheet('N1:P3', {'textFormat': {'fontSize': 9, 'foregroundColor': {'red': 0.33, 'green': 0.39, 'blue': 0.46}}, 'verticalAlignment': 'MIDDLE'})
    format_sheet('Q1:R3', {'textFormat': {'italic': True, 'fontSize': 9, 'foregroundColor': {'red': 0.2, 'green': 0.45, 'blue': 0.31}}, 'horizontalAlignment': 'CENTER', 'verticalAlignment': 'MIDDLE'})
    result_color = {'red': 0.88, 'green': 0.94, 'blue': 0.89} if revenue_today >= expenses_today else {'red': 0.98, 'green': 0.89, 'blue': 0.89}
    cards = [
        ('A5:D10', pale_green), ('E5:H10', pale_red), ('I5:L10', result_color), ('M5:R10', pale_amber),
    ]
    for cell_range, color in cards:
        format_sheet(cell_range, {'backgroundColor': color, 'verticalAlignment': 'MIDDLE'})
    format_sheet('A5:R5', {'textFormat': {'bold': True, 'fontSize': 10, 'foregroundColor': {'red': 0.12, 'green': 0.18, 'blue': 0.28}}})
    format_sheet('A6:L7', {'textFormat': {'bold': True, 'fontSize': 18}, 'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0 "THB"'}})
    format_sheet('M6:R7', {'textFormat': {'bold': True, 'fontSize': 18}, 'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0'}})
    format_sheet('A8:R8', {'textFormat': {'bold': True, 'fontSize': 10, 'foregroundColor': {'red': 0.2, 'green': 0.27, 'blue': 0.34}}})
    format_sheet('A9:L10', {'textFormat': {'bold': True, 'fontSize': 16}, 'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0 "THB"'}})
    format_sheet('M9:R10', {'textFormat': {'bold': True, 'fontSize': 16}, 'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0'}})
    format_sheet('O12:R22', {'backgroundColor': pale_blue, 'verticalAlignment': 'MIDDLE'})
    format_sheet('O12:R12', {'textFormat': {'bold': True, 'fontSize': 11}})
    format_sheet('O14:R17', {'textFormat': {'bold': True, 'fontSize': 18, 'foregroundColor': {'red': 0.05, 'green': 0.12, 'blue': 0.23}}, 'horizontalAlignment': 'CENTER', 'wrapStrategy': 'WRAP'})
    format_sheet('O18:R18', {'textFormat': {'fontSize': 9, 'foregroundColor': {'red': 0.33, 'green': 0.39, 'blue': 0.46}}})
    format_sheet('O19:R20', {'backgroundColor': pale_green, 'textFormat': {'bold': True, 'fontSize': 10, 'foregroundColor': {'red': 0.1, 'green': 0.42, 'blue': 0.22}}, 'verticalAlignment': 'MIDDLE'})
    format_sheet('O21:R22', {'backgroundColor': pale_green, 'textFormat': {'fontSize': 9, 'foregroundColor': {'red': 0.1, 'green': 0.42, 'blue': 0.22}}})
    format_sheet('A26:I26', {'backgroundColor': pale_red, 'textFormat': {'bold': True, 'fontSize': 12}, 'verticalAlignment': 'MIDDLE'})
    format_sheet('A27:I27', {'backgroundColor': {'red': 0.96, 'green': 0.97, 'blue': 0.98}, 'textFormat': {'bold': True, 'fontSize': 9}, 'horizontalAlignment': 'CENTER'})
    format_sheet('A28:I34', {'verticalAlignment': 'MIDDLE'})
    format_sheet('E28:F34', {'backgroundColor': pale_amber, 'horizontalAlignment': 'CENTER'})
    format_sheet('J26:N26', {'backgroundColor': pale_blue, 'textFormat': {'bold': True, 'fontSize': 12}, 'verticalAlignment': 'MIDDLE'})
    format_sheet('J28:N30', {'verticalAlignment': 'MIDDLE'})
    format_sheet('N28:N28', {'textFormat': {'bold': True, 'foregroundColor': {'red': 0.1, 'green': 0.45, 'blue': 0.22}}, 'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0 "THB"'}})
    format_sheet('N29:N30', {'textFormat': {'bold': True, 'foregroundColor': {'red': 0.8, 'green': 0.15, 'blue': 0.15}}, 'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0 "THB"'}})
    format_sheet('J32:N34', {'backgroundColor': pale_red, 'verticalAlignment': 'MIDDLE'})
    format_sheet('J32:N32', {'textFormat': {'bold': True, 'fontSize': 11, 'foregroundColor': {'red': 0.75, 'green': 0.14, 'blue': 0.14}}})
    format_sheet('J33:N34', {'textFormat': {'fontSize': 9, 'foregroundColor': {'red': 0.65, 'green': 0.2, 'blue': 0.2}}})
    format_sheet('O26:R26', {'backgroundColor': pale_amber, 'textFormat': {'bold': True, 'fontSize': 12}, 'verticalAlignment': 'MIDDLE'})
    format_sheet('O28:R34', {'backgroundColor': {'red': 1, 'green': 1, 'blue': 1}, 'textFormat': {'fontSize': 10}, 'verticalAlignment': 'MIDDLE'})
    format_sheet('A37:R37', {'backgroundColor': light_green, 'textFormat': {'fontSize': 9, 'foregroundColor': {'red': 0.25, 'green': 0.4, 'blue': 0.32}}, 'verticalAlignment': 'MIDDLE'})
    format_sheet('T1:V45', {'textFormat': {'foregroundColor': {'red': 1, 'green': 1, 'blue': 1}}})
    if hasattr(ws, 'batch_format'):
        ws.batch_format(format_requests)
    else:
        for request in format_requests:
            ws.format(request['range'], request['format'])
    return {'critical': len(critical), 'reminders': len(reminders)}


def refresh_overview_safely(client_cfg):
    """Keep dashboard refresh useful but never let it block a confirmed operation."""
    try:
        return refresh_overview(client_cfg)
    except Exception as exc:
        logger.warning('overview_refresh_failed', extra={'error_type': type(exc).__name__})
        return None


def upcoming_reminders(sh, now, days_limit=14, limit=5):
    try:
        rows = sh.worksheet('Напоминания').get_all_records()
    except Exception:
        return []
    reminders = []
    for row in rows:
        expiry = str(row.get('Дата окончания', '')).strip()
        title = str(row.get('Название', '')).strip()
        if not expiry or not title:
            continue
        try:
            left = days_until(expiry, now=now)
        except Exception:
            continue
        if 0 <= left <= days_limit:
            reminders.append((left, title, expiry))
    return sorted(reminders)[:limit]


def evening_summary(client_cfg):
    """Короткая сводка для владельца в 20:00 по Бангкоку."""
    if not gc:
        return
    try:
        tz = pytz.timezone('Asia/Bangkok')
        now = datetime.datetime.now(tz)
        date_today = now.strftime('%Y-%m-%d')
        month_prefix = now.strftime('%Y-%m')
        sh = gc.open_by_key(client_cfg['sheet_id'])

        revenue_today = revenue_month = expenses_today = expenses_month = 0.0
        try:
            rows = sh.worksheet('Выручка').get_all_records()
            revenue_today = sum(_money(row.get('Gross Sales')) for row in rows if str(row.get('Дата', '')).startswith(date_today))
            revenue_month = sum(_money(row.get('Gross Sales')) for row in rows if str(row.get('Дата', '')).startswith(month_prefix))
        except Exception:
            pass
        try:
            rows = sh.worksheet('Расходы').get_all_records()
            expenses_today = sum(_money(row.get('Сумма (THB)')) for row in rows if str(row.get('Дата', '')).startswith(date_today))
            expenses_month = sum(_money(row.get('Сумма (THB)')) for row in rows if str(row.get('Дата', '')).startswith(month_prefix))
        except Exception:
            pass

        balance_today = revenue_today - expenses_today
        msg = (
            f"🌙 Вечерняя сводка · {now.strftime('%d.%m.%Y')}\n\n"
            f"💰 Выручка сегодня: {revenue_today:,.0f} THB\n"
            f"💸 Расходы сегодня: {expenses_today:,.0f} THB\n"
            f"📈 Разница за день: {balance_today:+,.0f} THB\n"
            f"📊 За месяц: выручка {revenue_month:,.0f} · расходы {expenses_month:,.0f} THB"
        )
        reminders = upcoming_reminders(sh, now)
        if reminders:
            msg += '\n\n🔔 Ближайшие напоминания:'
            for left, title, expiry in reminders:
                when = 'сегодня' if left == 0 else f'через {left} дн.'
                msg += f"\n• {title} — {when} ({expiry})"
        else:
            msg += '\n\n🔔 Ближайших напоминаний нет.'
        notify_owner(client_cfg, msg)
    except Exception as exc:
        logger.error('evening_summary_failed', extra={'error_type': type(exc).__name__})


def reminders_report(client_cfg):
    if not gc:
        return
    try:
        now = datetime.datetime.now(pytz.timezone('Asia/Bangkok'))
        reminders = upcoming_reminders(gc.open_by_key(client_cfg['sheet_id']), now, days_limit=7, limit=12)
        if reminders:
            lines = ['🔔 Напоминания на ближайшие 7 дней:']
            for left, title, expiry in reminders:
                when = 'сегодня' if left == 0 else f'через {left} дн.'
                lines.append(f"• {title} — {when} ({expiry})")
            msg = '\n'.join(lines)
        else:
            msg = '🔔 На ближайшие 7 дней напоминаний нет.'
        notify_owner(client_cfg, msg)
    except Exception as exc:
        logger.error('reminders_report_failed', extra={'error_type': type(exc).__name__})


def owner_menu(client_cfg):
    notify_owner(client_cfg, 'Постоянное меню находится внизу чата. Нажмите «Отчёты», чтобы открыть его.')


def detailed_report(client_cfg):
    """Развёрнутый отчёт по запросу владельца."""
    if not gc:
        return
    try:
        tz = pytz.timezone('Asia/Bangkok')
        now = datetime.datetime.now(tz)
        date_today = now.strftime('%Y-%m-%d')
        yesterday = (now - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
        sh = gc.open_by_key(client_cfg['sheet_id'])
        try:
            all_rows = sh.worksheet('Остатки').get_all_records()
            today_rows = [row for row in all_rows if str(row.get('Дата', '')) == date_today] or all_rows[-50:]
        except Exception:
            today_rows = []
        try:
            rows_exp = sh.worksheet('Расходы').get_all_records()
            total_yesterday = sum(_money(row.get('Сумма (THB)')) for row in rows_exp if str(row.get('Дата', '')) == yesterday)
            total_month = sum(_money(row.get('Сумма (THB)')) for row in rows_exp if str(row.get('Дата', '')).startswith(now.strftime('%Y-%m')))
        except Exception:
            total_yesterday = total_month = 0.0
        stock_text = '\n'.join(
            f"{row.get('Продукт', '')} | Холодильник: {row.get('Холодильник', '')} | Морозилка: {row.get('Морозилка', '')} | {row.get('Примечание', '')}"
            for row in today_rows if row.get('Продукт')
        )
        report = ask_openai(
            "Ты аналитик кафе. Составь подробный отчёт только на русском для владельца. Будь конкретным и компактным. Формат: 📋 Подробный отчёт по кофейне [дата]; 🔴 ЗАКОНЧИЛОСЬ / КРИТИЧНО; 🟡 МАЛО ОСТАЛОСЬ; 💰 РАСХОДЫ ВЧЕРА; 📊 РАСХОДЫ ЗА МЕСЯЦ; 💡 РЕКОМЕНДАЦИИ.",
            f"Дата: {date_today}\nОстатки:\n{stock_text}\nРасходы вчера: {total_yesterday} THB\nРасходы за месяц: {total_month} THB",
            1000,
        )
        reminders = upcoming_reminders(sh, now, days_limit=7)
        if reminders:
            report += '\n\n🔔 ВАЖНЫЕ ДОКУМЕНТЫ:'
            for left, title, expiry in reminders:
                report += f"\n⚠️ {title} — истекает через {left} дн. ({expiry})"
        notify_owner(client_cfg, report)
    except Exception as exc:
        logger.error('detailed_report_failed', extra={'error_type': type(exc).__name__})

# ── Webhook ──────────────────────────────────────────────────────
def process_line_event(destination, event):
    """Process one event claimed by the durable worker."""
    client_cfg = find_client(destination)
    if not client_cfg:
        raise ValueError(f"Unknown destination: {destination}")
    if event.get('type') != 'message':
        return
    if not SHEETS_ENABLED:
        raise RuntimeError('Google Sheets is not ready')
    source = event.get('source', {})
    if source.get('type') not in ('group', 'room'):
        allowed_owners = {client_cfg.get('owner_line_id'), client_cfg.get('owner_line_id_2')}
        if source.get('userId') not in allowed_owners:
            return
        _msg = event.get('message', {})
        command = _msg.get('text', '').lower().strip() if _msg.get('type') == 'text' else ''
        if command in ['сводка', 'отчет', 'отчёт', 'подробный отчёт', 'report']:
            detailed_report(client_cfg)
        elif command in ['деньги', 'финансы', 'money']:
            evening_summary(client_cfg)
        elif command in ['напоминания', 'reminders']:
            reminders_report(client_cfg)
        elif command in ['меню', 'menu']:
            owner_menu(client_cfg)
        elif command in ['неделя', 'week', 'недельная']:
            weekly_report(client_cfg)
        return
    msg = event.get('message', {})
    msg_type = msg.get('type')
    event_id = event.get('webhookEventId')
    current_bangkok = bangkok_now()
    date_only = current_bangkok.strftime("%Y-%m-%d")
    now_str = current_bangkok.strftime("%Y-%m-%d %H:%M")
    blob_api = get_line_blob_api(client_cfg['channel_access_token'])

    # ── Текст ──
    if msg_type == 'text':
        text = msg.get('text', '').strip()
        result = analyze_text(text, client_cfg)
        if result == 'IGNORE' or not result:
            return
        if result.startswith('ВАЖНО [ПРОБЛЕМА]'):
            save_проблемы(client_cfg['sheet_id'], text, result, now_str, event_id)
            refresh_overview_safely(client_cfg)
            notify_owner(client_cfg, result)
            return
        try:
            json_match = re.search(r'\{.*\}', result, re.DOTALL)
            if not json_match:
                return
            data = json.loads(json_match.group())
            if data['type'] == 'purchase':
                save_закупки(client_cfg['sheet_id'], data['items'], date_only, event_id)
                refresh_overview_safely(client_cfg)
                msg_text = "🛒 ЗАКУПКА записана:\n"
                for item in data['items']:
                    msg_text += f"- {item['product']}: {item['quantity']}\n"
                # уведомление в дайджесте 18:00
            elif data['type'] == 'stock':
                save_остатки(client_cfg['sheet_id'], data['items'], date_only, event_id)
                refresh_overview_safely(client_cfg)
                out = [i for i in data['items'] if i.get('note') in ['Out of stock','Exp today']]
                low = [i for i in data['items'] if i.get('note') == 'Low stock']
                msg_text = f"📦 ОСТАТКИ записаны ({len(data['items'])} позиций)\n"
                if out:
                    msg_text += "\n🔴 ЗАКОНЧИЛОСЬ / ИСТЕКАЕТ СЕГОДНЯ:\n"
                    for i in out: msg_text += f"- {i['product']}\n"
                if low:
                    msg_text += "\n🟡 МАЛО ОСТАЛОСЬ:\n"
                    for i in low: msg_text += f"- {i['product']}\n"
                notify_owner(client_cfg, msg_text)
            elif data['type'] == 'single_stock':
                save_одиночный_остаток(client_cfg['sheet_id'], data.get('product',''), data.get('amount',''), date_only, event_id)
                refresh_overview_safely(client_cfg)
                notify_owner(client_cfg, f"📦 Остаток записан:\n{data.get('product','')}: {data.get('amount','')}")
            elif data['type'] == 'text_expense':
                items = data.get('items', [])
                total = data.get('total', '')
                supplier = data.get('supplier', '')
                positions = ', '.join([i['description'] for i in items if i.get('description')])
                save_расходы(client_cfg['sheet_id'], [{'type': 'Закупка', 'description': positions, 'amount': total}], date_only, supplier, event_id=event_id, effect='text-expense')
                refresh_overview_safely(client_cfg)
                notify_owner(client_cfg, f"💸 РАСХОД записан:\nМагазин: {supplier}\nПозиции: {positions}\nИтого: {total} THB")
        except Exception as e:
            raise RuntimeError(f"Text handler error: {e}") from e

    # ── Фото ──
    elif msg_type == 'image':
        logger.info('image_processing_started', extra={'event_id': event_id})
        try:
            content = blob_api.get_message_content(msg.get('id'))
            image_data = base64.b64encode(content).decode('utf-8')
            result = analyze_image(image_data, client_cfg)
            if 'NOT_FINANCE' in result:
                return
            json_match = re.search(r'\{.*\}', result, re.DOTALL)
            if not json_match:
                return
            data = json.loads(json_match.group())
            doc_type = data.get('doc_type')
            logger.info('image_analysis_completed', extra={'event_id': event_id, 'document_type': doc_type or 'unknown'})
            if doc_type == 'shift':
                save_выручка(client_cfg['sheet_id'], data, date_only, data.get('note',''), event_id)
                refresh_overview_safely(client_cfg)
                diff = data.get('difference', 0)
                msg_text = f"💰 Смена #{data.get('shift','?')}\n"
                msg_text += f"📊 Выручка: {data.get('gross_sales','')} THB\n"
                msg_text += f"💵 Наличные: {data.get('cash','')} THB\n"
                msg_text += f"💳 Карта: {data.get('card','')} THB\n"
                msg_text += f"📱 QR: {data.get('qr','')} THB\n"
                msg_text += f"✅ Касса: {'+' if float(diff or 0) >= 0 else ''}{diff} THB"
                notify_owner(client_cfg, msg_text)
            elif doc_type == 'invoice':
                save_расходы(client_cfg['sheet_id'], data.get('items',[]), date_only, data.get('supplier',''), data.get('note',''), event_id, 'invoice-expense')
                check_price_drift(client_cfg['sheet_id'], data.get('items',[]), data.get('supplier',''), client_cfg, event_id)
                refresh_overview_safely(client_cfg)
                notify_owner(client_cfg, f"🧾 НАКЛАДНАЯ записана\nПоставщик: {data.get('supplier','—')}\nИтого: {data.get('total','—')} THB")
            elif doc_type == 'expense':
                save_расходы(client_cfg['sheet_id'], data.get('items',[]), date_only, data.get('supplier',''), data.get('note',''), event_id, 'receipt-expense')
                refresh_overview_safely(client_cfg)
                notify_owner(client_cfg, f"🛒 РАСХОД записан\nМагазин: {data.get('supplier','—')}\nИтого: {data.get('total','—')} THB")
            elif doc_type == 'salary':
                save_зарплаты(client_cfg['sheet_id'], [data], date_only, event_id)
                refresh_overview_safely(client_cfg)
                notify_owner(client_cfg, f"💼 ЗАРПЛАТА записана\nПолучатель: {data.get('recipient','—')}\nСумма: {data.get('amount','—')} THB")
            elif doc_type == 'reminder':
                save_напоминание(client_cfg['sheet_id'], data, date_only, event_id)
                refresh_overview_safely(client_cfg)
                notify_owner(client_cfg, f"📅 НАПОМИНАНИЕ записано\n📄 {data.get('title','—')}\n⏰ Истекает: {data.get('expiry_date','—')}")
            elif doc_type == 'notice':
                notify_owner(client_cfg, f"⚡️ ВАЖНОЕ УВЕДОМЛЕНИЕ\n\n{data.get('title','')}\n\n{data.get('content','')}")
            elif doc_type == 'bank_history':
                items = data.get('items', [])
                expenses = [i for i in items if i.get('type') == 'expense']
                salaries = [i for i in items if i.get('type') == 'salary']
                if expenses:
                    expense_rows = [{'type': 'Закупка', 'supplier': item.get('recipient',''), 'description': '', 'amount': item.get('amount',''), 'note': item.get('note','')} for item in expenses]
                    save_расходы(client_cfg['sheet_id'], expense_rows, date_only, '', event_id=event_id, effect='bank-expense')
                if salaries:
                    save_зарплаты(client_cfg['sheet_id'], salaries, date_only, event_id)
                if expenses or salaries:
                    refresh_overview_safely(client_cfg)
                msg = f"🏦 ТРАНЗАКЦИИ записаны ({len(items)} шт)\n"
                if expenses:
                    msg += f"💸 Расходы: {len(expenses)} шт\n"
                if salaries:
                    msg += f"💼 Зарплаты: {len(salaries)} шт"
                notify_owner(client_cfg, msg)
        except Exception as e:
            raise RuntimeError(f"Image handler error: {e}") from e


@app.route("/webhook", methods=['POST'])
def webhook():
    body = request.get_data(as_text=True)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        abort(400)

    destination = payload.get('destination', '')
    client_cfg = find_client(destination)
    if not client_cfg:
        logger.warning('webhook_unknown_destination')
        return 'OK'

    signature = request.headers.get('X-Line-Signature', '')
    handler = WebhookHandler(client_cfg['channel_secret'])
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    message_events = [event for event in payload.get('events', []) if event.get('type') == 'message']
    if not message_events:
        return 'OK'
    if not event_store or not DB_ENABLED:
        return {'status': 'database_unavailable'}, 503

    try:
        accepted, duplicates = event_store.register_events(destination, message_events)
    except Exception as exc:
        logger.error('event_registration_failed', extra={'error_type': type(exc).__name__})
        return {'status': 'event_registration_failed'}, 503

    logger.info('events_registered', extra={'accepted': accepted, 'duplicates': duplicates})
    return 'OK'

@app.route("/health", methods=['GET'])
def health():
    return {"status": "ok", "clients": len(CLIENTS), "sheets": SHEETS_ENABLED}, 200


@app.route("/ready", methods=['GET'])
def ready():
    database_ready = bool(event_store and DB_ENABLED and event_store.ping())
    ready_state = bool(CLIENTS) and SHEETS_ENABLED and database_ready
    payload = {
        "status": "ready" if ready_state else "not_ready",
        "clients": len(CLIENTS),
        "sheets": SHEETS_ENABLED,
        "database": database_ready,
    }
    return payload, 200 if ready_state else 503

# ── Планировщик ──────────────────────────────────────────────────
try:
    if os.environ.get('DISABLE_SCHEDULER') == '1':
        raise RuntimeError('Scheduler disabled for this process')
    scheduler = BackgroundScheduler(timezone=pytz.timezone('Asia/Bangkok'))
    for bot_id, cfg in CLIENTS.items():
        if cfg.get('sheet_id') and gc:
            scheduler.add_job(evening_summary, 'cron', hour=20, minute=0, args=[cfg], id=f"evening_{bot_id}")
            scheduler.add_job(weekly_report, 'cron', day_of_week='sun', hour=18, minute=0, args=[cfg], id=f"weekly_{bot_id}")
    scheduler.start()
    logger.info('scheduler_started')
except Exception as exc:
    logger.info('scheduler_not_started', extra={'error_type': type(exc).__name__})

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
