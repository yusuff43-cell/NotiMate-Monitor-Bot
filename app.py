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
    MessageAction,
    QuickReply,
    QuickReplyItem,
    TextMessage,
)
from openai import APIStatusError, OpenAI, RateLimitError
import gspread
from google.oauth2.service_account import Credentials
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

from core import bangkok_date, bangkok_now, client_prompt_context, days_until, validate_clients
from event_store import PostgresEventStore

app = Flask(__name__)

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
    except Exception as e:
        print(f"Database init failed: {e}")

SHEETS_ENABLED = False
gc = None
try:
    creds_json = json.loads(os.environ['GOOGLE_CREDENTIALS'])
    scopes = ['https://www.googleapis.com/auth/spreadsheets']
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    gc = gspread.authorize(creds)
    SHEETS_ENABLED = True
except Exception as e:
    print(f"Sheets init skipped: {e}")

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
            return result
        except (RateLimitError, APIStatusError) as exc:
            status_code = getattr(exc, 'status_code', None)
            retryable = isinstance(exc, RateLimitError) or status_code in {408, 409, 429} or (status_code and status_code >= 500)
            if retryable and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise

# ── Google Sheets helpers ────────────────────────────────────────
def get_or_create_sheet(sh, name, headers):
    try:
        return sh.worksheet(name)
    except:
        ws = sh.add_worksheet(title=name, rows=1000, cols=len(headers))
        ws.append_row(headers)
        return ws

def get_last_date(ws):
    try:
        col = ws.col_values(1)
        for val in reversed(col):
            if val and val != 'Дата':
                return val
    except:
        pass
    return None

def save_остатки(sheet_id, items, date_str):
    if not gc: return
    sh = gc.open_by_key(sheet_id)
    headers = ['Дата', 'Категория', 'Продукт', 'Холодильник', 'Морозилка', 'Примечание']
    ws = get_or_create_sheet(sh, 'Остатки', headers)
    last_date = get_last_date(ws)
    last_category = None
    for i, item in enumerate(items):
        date_cell = date_str if (i == 0 and last_date != date_str) else ''
        category = item.get('category', '')
        category_cell = category if category != last_category else ''
        if category:
            last_category = category
        ws.append_row([date_cell, category_cell, item.get('product',''), item.get('fridge',''), item.get('freezer',''), item.get('note','')])

def save_закупки(sheet_id, items, date_str):
    if not gc: return
    sh = gc.open_by_key(sheet_id)
    headers = ['Дата', 'Продукт', 'Количество']
    ws = get_or_create_sheet(sh, 'Закупки', headers)
    last_date = get_last_date(ws)
    # Получаем уже записанные сегодня продукты
    existing = ws.get_all_records()
    # Дата пишется только в первую строку, остальные пустые - берём все записи начиная с последней даты
    last_date_in_sheet = get_last_date(ws)
    today_names = []
    if last_date_in_sheet and last_date_in_sheet.startswith(date_str):
        # Берём только записи последней группы (после последней даты)
        last_idx = 0
        for idx, r in enumerate(existing):
            if str(r.get('Дата','')).startswith(date_str):
                last_idx = idx
                break
        today_names = [r.get('Продукт','').lower().strip() for r in existing[last_idx:] if r.get('Продукт','')]
    incoming_names = [i.get('product','').lower().strip() for i in items]
    # Если входящий список совпадает с сегодняшним на 60%+ — это обновление
    if today_names and incoming_names:
        matches = sum(1 for n in incoming_names if any(n[:4] in t or t[:4] in n for t in today_names if len(t) > 3))
        overlap = matches / len(incoming_names)
        if overlap >= 0.6:
            # Добавляем только действительно новые позиции
            new_items = [i for i in items if not any(i.get('product','').lower().strip()[:4] in t or t[:4] in i.get('product','').lower().strip() for t in today_names if len(t) > 3)]
        else:
            new_items = items
    else:
        new_items = items

    if not new_items:
        print('No new items, skipping')
        return
    for i, item in enumerate(new_items):
        date_cell = date_str if (i == 0 and last_date != date_str) else ''
        ws.append_row([date_cell, item.get('product',''), item.get('quantity','')])

def save_расходы(sheet_id, items, date_str, supplier, note=''):
    if not gc: return
    sh = gc.open_by_key(sheet_id)
    headers = ['Дата', 'Тип', 'Поставщик/Магазин', 'Позиция', 'Сумма (THB)', 'Примечание']
    ws = get_or_create_sheet(sh, 'Расходы', headers)
    for item in items:
        clean_amount = str(item.get('amount','') or '').replace('฿','').replace('B','').replace(',','').strip()
        ws.append_row([date_str, item.get('type','Закупка'), supplier, item.get('description',''), clean_amount, note])

def save_выручка(sheet_id, data, date_str, note=''):
    if not gc: return
    sh = gc.open_by_key(sheet_id)
    headers = ['Дата', 'Смена', 'Gross Sales', 'Наличные', 'Карта', 'QR', 'Примечание']
    ws = get_or_create_sheet(sh, 'Выручка', headers)
    ws.append_row([date_str, data.get('shift',''), data.get('gross_sales',''), data.get('cash',''), data.get('card',''), data.get('qr',''), note])

def save_проблемы(sheet_id, text, result, date_str):
    if not gc: return
    sh = gc.open_by_key(sheet_id)
    headers = ['Дата', 'Сообщение', 'Перевод и совет']
    ws = get_or_create_sheet(sh, 'Проблемы', headers)
    ws.append_row([date_str, text, result])

def notify_owner(client_cfg, msg, with_actions=False):
    try:
        api = get_line_api(client_cfg['channel_access_token'])
        quick_reply = owner_quick_actions() if with_actions else None
        recipients = [client_cfg['owner_line_id']]
        if client_cfg.get('owner_line_id_2'):
            recipients.append(client_cfg['owner_line_id_2'])
        for recipient in recipients:
            api.push_message(PushMessageRequest(
                to=recipient,
                messages=[TextMessage(text=msg, quick_reply=quick_reply)],
            ))
        return True
    except Exception as e:
        print(f"Notify error: {e}")
        return False

# ── Анализ фото ──────────────────────────────────────────────────
def check_price_drift(sheet_id, items, supplier, client_cfg):
    """Проверяет дрейф цен и уведомляет если цена выросла >10%"""
    if not gc:
        return
    try:
        sh = gc.open_by_key(sheet_id)
        headers = ['Дата', 'Поставщик', 'Позиция', 'Цена (THB)']
        ws = get_or_create_sheet(sh, 'Цены', headers)
        rows = ws.get_all_records()
        alerts = []
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
            # Записываем новую цену
            ws.append_row([date_today, supplier, name, price])
            # Проверяем дрейф
            if prev_price and prev_price > 0 and price > 0:
                drift = (price - prev_price) / prev_price * 100
                if drift >= 10:
                    alerts.append(f"- {name}: {prev_price:.0f} → {price:.0f} THB (+{drift:.0f}%)")
        if alerts:
            msg = f"⚠️ ДРЕЙФ ЦЕН от {supplier}:\n"
            msg += "\n".join(alerts)
            msg += "\n\n💡 Проверьте накладную — поставщик поднял цены."
            notify_owner(client_cfg, msg)
    except Exception as e:
        print(f"Price drift error: {e}")


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
    except Exception as e:
        print(f"Weekly report error: {e}")


def _money(value):
    try:
        return float(str(value or 0).replace('฿', '').replace(',', '').replace('B', '').strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def owner_quick_actions():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label='Подробный отчёт', text='подробный отчёт')),
        QuickReplyItem(action=MessageAction(label='Деньги', text='деньги')),
        QuickReplyItem(action=MessageAction(label='Напоминания', text='напоминания')),
    ])


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
        notify_owner(client_cfg, msg, with_actions=True)
    except Exception as e:
        print(f"Evening summary error: {e}")


def reminders_report(client_cfg):
    if not gc:
        return
    try:
        now = datetime.datetime.now(pytz.timezone('Asia/Bangkok'))
        reminders = upcoming_reminders(gc.open_by_key(client_cfg['sheet_id']), now, days_limit=60, limit=12)
        if reminders:
            lines = ['🔔 Напоминания на ближайшие 60 дней:']
            for left, title, expiry in reminders:
                when = 'сегодня' if left == 0 else f'через {left} дн.'
                lines.append(f"• {title} — {when} ({expiry})")
            msg = '\n'.join(lines)
        else:
            msg = '🔔 На ближайшие 60 дней напоминаний нет.'
        notify_owner(client_cfg, msg, with_actions=True)
    except Exception as e:
        print(f"Reminders report error: {e}")


def owner_menu(client_cfg):
    notify_owner(client_cfg, 'Выберите нужный отчёт:', with_actions=True)


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
        notify_owner(client_cfg, report, with_actions=True)
    except Exception as e:
        print(f"Detailed report error: {e}")

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
            save_проблемы(client_cfg['sheet_id'], text, result, now_str)
            notify_owner(client_cfg, result)
            return
        try:
            json_match = re.search(r'\{.*\}', result, re.DOTALL)
            if not json_match:
                return
            data = json.loads(json_match.group())
            if data['type'] == 'purchase':
                save_закупки(client_cfg['sheet_id'], data['items'], date_only)
                msg_text = "🛒 ЗАКУПКА записана:\n"
                for item in data['items']:
                    msg_text += f"- {item['product']}: {item['quantity']}\n"
                # уведомление в дайджесте 18:00
            elif data['type'] == 'stock':
                save_остатки(client_cfg['sheet_id'], data['items'], date_only)
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
                if gc:
                    sh = gc.open_by_key(client_cfg['sheet_id'])
                    ws = get_or_create_sheet(sh, 'Остатки', ['Дата','Категория','Продукт','Холодильник','Морозилка','Примечание'])
                    ws.append_row([date_only, '', data.get('product',''), data.get('amount',''), '', ''])
                notify_owner(client_cfg, f"📦 Остаток записан:\n{data.get('product','')}: {data.get('amount','')}")
            elif data['type'] == 'text_expense':
                items = data.get('items', [])
                total = data.get('total', '')
                supplier = data.get('supplier', '')
                positions = ', '.join([i['description'] for i in items if i.get('description')])
                if gc:
                    sh = gc.open_by_key(client_cfg['sheet_id'])
                    ws = get_or_create_sheet(sh, 'Расходы', ['Дата','Тип','Поставщик/Магазин','Позиция','Сумма (THB)','Примечание'])
                    clean_total = str(total or '').replace('฿','').replace('B','').replace(',','').strip()
                    ws.append_row([date_only, 'Закупка', supplier, positions, clean_total, ''])
                notify_owner(client_cfg, f"💸 РАСХОД записан:\nМагазин: {supplier}\nПозиции: {positions}\nИтого: {total} THB")
        except Exception as e:
            raise RuntimeError(f"Text handler error: {e}") from e

    # ── Фото ──
    elif msg_type == 'image':
        print(f'Image received, processing...')
        try:
            content = blob_api.get_message_content(msg.get('id'))
            image_data = base64.b64encode(content).decode('utf-8')
            result = analyze_image(image_data, client_cfg)
            print(f'Image result: {result[:200]}')
            if 'NOT_FINANCE' in result:
                return
            json_match = re.search(r'\{.*\}', result, re.DOTALL)
            if not json_match:
                return
            data = json.loads(json_match.group())
            doc_type = data.get('doc_type')
            if doc_type == 'shift':
                save_выручка(client_cfg['sheet_id'], data, date_only, data.get('note',''))
                diff = data.get('difference', 0)
                msg_text = f"💰 Смена #{data.get('shift','?')}\n"
                msg_text += f"📊 Выручка: {data.get('gross_sales','')} THB\n"
                msg_text += f"💵 Наличные: {data.get('cash','')} THB\n"
                msg_text += f"💳 Карта: {data.get('card','')} THB\n"
                msg_text += f"📱 QR: {data.get('qr','')} THB\n"
                msg_text += f"✅ Касса: {'+' if float(diff or 0) >= 0 else ''}{diff} THB"
                notify_owner(client_cfg, msg_text)
            elif doc_type == 'invoice':
                save_расходы(client_cfg['sheet_id'], data.get('items',[]), date_only, data.get('supplier',''), data.get('note',''))
                check_price_drift(client_cfg['sheet_id'], data.get('items',[]), data.get('supplier',''), client_cfg)
                notify_owner(client_cfg, f"🧾 НАКЛАДНАЯ записана\nПоставщик: {data.get('supplier','—')}\nИтого: {data.get('total','—')} THB")
            elif doc_type == 'expense':
                save_расходы(client_cfg['sheet_id'], data.get('items',[]), date_only, data.get('supplier',''), data.get('note',''))
                notify_owner(client_cfg, f"🛒 РАСХОД записан\nМагазин: {data.get('supplier','—')}\nИтого: {data.get('total','—')} THB")
            elif doc_type == 'salary':
                if gc:
                    sh = gc.open_by_key(client_cfg['sheet_id'])
                    ws = get_or_create_sheet(sh, 'Зарплаты', ['Дата','Получатель','Сумма (THB)','Примечание'])
                    ws.append_row([date_only, data.get('recipient',''), data.get('amount',''), data.get('note','')])
                notify_owner(client_cfg, f"💼 ЗАРПЛАТА записана\nПолучатель: {data.get('recipient','—')}\nСумма: {data.get('amount','—')} THB")
            elif doc_type == 'reminder':
                if gc:
                    sh = gc.open_by_key(client_cfg['sheet_id'])
                    ws = get_or_create_sheet(sh, 'Напоминания', ['Название', 'Дата окончания', 'Дата добавления', 'Примечание'])
                    ws.append_row([data.get('title',''), data.get('expiry_date',''), date_only, data.get('note','')])
                notify_owner(client_cfg, f"📅 НАПОМИНАНИЕ записано\n📄 {data.get('title','—')}\n⏰ Истекает: {data.get('expiry_date','—')}")
            elif doc_type == 'notice':
                notify_owner(client_cfg, f"⚡️ ВАЖНОЕ УВЕДОМЛЕНИЕ\n\n{data.get('title','')}\n\n{data.get('content','')}")
            elif doc_type == 'bank_history':
                items = data.get('items', [])
                expenses = [i for i in items if i.get('type') == 'expense']
                salaries = [i for i in items if i.get('type') == 'salary']
                if gc and expenses:
                    sh = gc.open_by_key(client_cfg['sheet_id'])
                    ws = get_or_create_sheet(sh, 'Расходы', ['Дата','Тип','Поставщик/Магазин','Позиция','Сумма (THB)','Примечание'])
                    for item in expenses:
                        ws.append_row([date_only, 'Закупка', item.get('recipient',''), '', item.get('amount',''), item.get('note','')])
                if gc and salaries:
                    sh = gc.open_by_key(client_cfg['sheet_id'])
                    ws = get_or_create_sheet(sh, 'Зарплаты', ['Дата','Получатель','Сумма (THB)','Примечание'])
                    for item in salaries:
                        ws.append_row([date_only, item.get('recipient',''), item.get('amount',''), item.get('note','')])
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
        print(f"Unknown destination: {destination}")
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
        print(f"Event registration failed: {type(exc).__name__}: {exc}")
        return {'status': 'event_registration_failed'}, 503

    print(f"Events registered: accepted={accepted} duplicates={duplicates}")
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
    print("Scheduler started")
except Exception as e:
    print(f"Scheduler error: {e}")

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
