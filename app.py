import os
import json
import re
import time
import base64
import datetime
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage
import anthropic
import gspread
from google.oauth2.service_account import Credentials
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

app = Flask(__name__)

# ── Глобальные сервисы ──────────────────────────────────────────
claude = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])

with open('clients.json', 'r', encoding='utf-8') as f:
    CLIENTS = json.load(f)

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

def get_line_api(token: str) -> LineBotApi:
    if token not in _line_api_cache:
        _line_api_cache[token] = LineBotApi(token)
    return _line_api_cache[token]

def find_client(destination: str):
    return CLIENTS.get(destination)

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
    for i, item in enumerate(items):
        date_cell = date_str if (i == 0 and last_date != date_str) else ''
        ws.append_row([date_cell, item.get('product',''), item.get('quantity','')])

def save_расходы(sheet_id, items, date_str, supplier, note=''):
    if not gc: return
    sh = gc.open_by_key(sheet_id)
    headers = ['Дата', 'Тип', 'Поставщик/Магазин', 'Позиция', 'Сумма (THB)', 'Примечание']
    ws = get_or_create_sheet(sh, 'Расходы', headers)
    for item in items:
        ws.append_row([date_str, item.get('type','Закупка'), supplier, item.get('description',''), item.get('amount',''), note])

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

def notify_owner(client_cfg, msg):
    try:
        api = get_line_api(client_cfg['channel_access_token'])
        api.push_message(client_cfg['owner_line_id'], TextSendMessage(text=msg))
    except Exception as e:
        print(f"Notify error: {e}")

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
        date_today = datetime.datetime.now().strftime('%Y-%m-%d')
        for item in items:
            name = item.get('description', '').strip()
            try:
                price = float(str(item.get('amount', 0) or 0).replace('฿','').replace(',','').strip() or 0)
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
    lang = client_cfg.get('notification_language', 'russian')
    lang_map = {'russian': 'Отвечай ТОЛЬКО на русском языке.', 'thai': 'ตอบเป็นภาษาไทยเท่านั้น', 'english': 'Reply in English only.'}
    lang_instruction = lang_map.get(lang, lang_map['russian'])
    for attempt in range(3):
        try:
            response = claude.messages.create(
                model="claude-haiku-4-5",
                max_tokens=2000,
                system=f"""Ты анализируешь фото документов для кофейни. {lang_instruction}

Если это SHIFT REPORT (содержит Shift number, Gross sales, Cash drawer):
Верни ТОЛЬКО JSON: {{"doc_type":"shift","shift":"номер смены","gross_sales":число,"cash":число,"card":число,"qr":число,"difference":число,"note":""}}

Если это НАКЛАДНАЯ от поставщика:
Верни ТОЛЬКО JSON: {{"doc_type":"invoice","supplier":"поставщик","items":[{{"description":"позиция на русском","amount":"сумма"}}],"total":"итого","note":""}}

Если это ЧЕК или фото покупки:
Верни ТОЛЬКО JSON: {{"doc_type":"expense","supplier":"магазин","items":[{{"description":"что купили на русском","amount":"сумма"}}],"total":"итого","note":""}}

Если это БАНКОВСКИЙ ПЕРЕВОД сотруднику (Transfer Completed, KBIZ, SCB):
Верни ТОЛЬКО JSON: {{"doc_type":"salary","recipient":"имя получателя","amount":0,"note":""}}

Если это ОБЪЯВЛЕНИЕ или УВЕДОМЛЕНИЕ:
Верни ТОЛЬКО JSON: {{"doc_type":"notice","title":"заголовок","content":"перевод","note":""}}

Если это СКРИНШОТ ИСТОРИИ ТРАНЗАКЦИЙ из банковского приложения (Transaction history, Payment, Transfer, Top up):
Верни ТОЛЬКО JSON: {{"doc_type":"bank_history","items":[{{"type":"expense","recipient":"получатель","amount":0,"note":""}}]}}
Правила:
- Payment/Scan to pay → type=expense
- Transfer PromptPay к физлицу (имя) → type=salary  
- Top up PromptPay Wallet → type=expense
- Игнорируй строки без суммы

Скриншоты магазинов, ценники, фото продуктов без чека — верни: NOT_FINANCE
Если не финансовый документ — верни: NOT_FINANCE""",
                messages=[{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_data}}, {"type": "text", "text": "Проанализируй"}]}]
            )
            return response.content[0].text.strip()
        except anthropic.APIStatusError as e:
            if e.status_code == 529 and attempt < 2:
                time.sleep(10)
                continue
            raise e

# ── Анализ текста ────────────────────────────────────────────────
def analyze_text(text, client_cfg):
    lang = client_cfg.get('notification_language', 'russian')
    lang_map = {'russian': 'Отвечай ТОЛЬКО на русском языке. Переводи тайский и английский на русский.', 'thai': 'ตอบเป็นภาษาไทยเท่านั้น', 'english': 'Reply in English only.'}
    lang_instruction = lang_map.get(lang, lang_map['russian'])
    for attempt in range(3):
        try:
            response = claude.messages.create(
                model="claude-haiku-4-5",
                max_tokens=8096,
                system=f"""КРИТИЧЕСКИ ВАЖНО: Только анализируй сообщения по правилам. Если не подходит — верни ТОЛЬКО: IGNORE
{lang_instruction}

СЛОВАРЬ: Clear/Clear croissant=Масляный круассан, Chocolate=Шоколадный круассан, Almond=Миндальный круассан, Ham Cheese=Круассан с ветчиной и сыром, Cheesecake=Чизкейк, Biscoff cheesecake=Бискофф чизкейк, Cheese pancakes=Сырники, Mango cheese pancakes=Манговые сырники, Cucumber cheese pancakes=Огуречные сырники, Crepes=Шпинатные блинчики, Pancakes=Панкейки, Crepes burger=Блины для бургера, Pannacotta=Панна-котта, Chocolate mousse=Шоколадный мусс, Salted Caramel=Солёная карамель, Bounty=Баунти, Halva=Халва, Marzipan=Марципан, Brownie=Брауни, Banana bread=Банановый хлеб, Muffin=Маффин, Snickers=Сникерс, Napoleons=Наполеон, Sourdough=Хлеб на закваске (для брускет), Dragon fruit=Драгон фрут, Salmon=Лосось, Yogurt=Йогурт, Açaí=Асаи

ТИПЫ СООБЩЕНИЙ:

1. ЗАКУПКИ - "we need", "for tomorrow", "need", "order" в начале БЕЗ цены
Если есть сумма (฿, =число฿) — это РАСХОД (тип 4), не закупка
Верни ТОЛЬКО JSON: {{"type":"purchase","items":[{{"product":"название на русском","quantity":"количество"}}]}}

2. ОСТАТКИ - начинаются с "Update"
Верни ТОЛЬКО JSON: {{"type":"stock","items":[{{"category":"Круассаны/Десерты/Блины и сырники/Макаруны/Начинки/Другое","product":"название на русском","fridge":"","freezer":"","note":""}}]}}
Правила note: "Out of stock" если всё 0, "Low stock" если 1-2 шт, "Exp today" если помечено

3. ОСТАТОК ОДНОЙ ПОЗИЦИИ - "товар have/has количество" или "товар количество г/pcs"
Верни ТОЛЬКО JSON: {{"type":"single_stock","product":"название на русском","amount":"количество"}}

4. РАСХОД - покупка с подтверждением (bought, paid, total, ฿, -)
Триггеры: "bought","paid","total","spent","-число฿ for", минус перед суммой, "=число฿", просто "число฿" или "฿число", формат "N товар =сумма฿" (например: 1 ice =35฿, 2 bag of ice =70฿)
Верни ТОЛЬКО JSON: {{"type":"text_expense","supplier":"магазин","items":[{{"description":"что купили на русском"}}],"total":"сумма"}}

5. ПРОБЛЕМА - поломки, аварии, инциденты
Верни: ВАЖНО [ПРОБЛЕМА]: [описание]\\n💡 Совет: [совет]

Если не подходит — верни только: IGNORE""",
                messages=[{"role": "user", "content": text}]
            )
            return response.content[0].text.strip()
        except anthropic.APIStatusError as e:
            if e.status_code == 529 and attempt < 2:
                time.sleep(10)
                continue
            raise e

# ── Утренняя сводка ──────────────────────────────────────────────
_last_report = {}
def morning_report(client_cfg):
    global _last_report
    bot_id = client_cfg.get("owner_line_id","")
    today = datetime.datetime.now(pytz.timezone("Asia/Bangkok")).strftime("%Y-%m-%d")
    if _last_report.get(bot_id) == today:
        print("Morning report already sent today, skipping")
        return
    _last_report[bot_id] = today
    try:
        tz = pytz.timezone('Asia/Bangkok')
        now = datetime.datetime.now(tz)
        date_today = now.strftime("%Y-%m-%d")
        yesterday = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        sh = gc.open_by_key(client_cfg['sheet_id'])
        today_rows = []
        try:
            ws_ost = sh.worksheet('Остатки')
            all_rows = ws_ost.get_all_records()
            today_rows = [r for r in all_rows if str(r.get('Дата','')) == date_today]
            if not today_rows:
                today_rows = all_rows[-50:] if len(all_rows) > 50 else all_rows
        except: pass
        total_yesterday = 0
        total_recent = 0
        try:
            ws_exp = sh.worksheet('Расходы')
            rows_exp = ws_exp.get_all_records()
            yest_exp = [r for r in rows_exp if str(r.get('Дата','')) == yesterday]
            total_yesterday = sum(float(str(r.get('Сумма (THB)',0) or 0).replace('฿','').replace(',','').strip() or 0) for r in yest_exp)
            month_start = now.strftime('%Y-%m')
            month_exp = [r for r in rows_exp if str(r.get('Дата','')).startswith(month_start)]
            total_recent = sum(float(str(r.get('Сумма (THB)',0) or 0).replace('฿','').replace(',','').strip() or 0) for r in month_exp)
        except: pass
        остатки_текст = "\n".join([f"{r.get('Продукт','')} | Холодильник: {r.get('Холодильник','')} | Морозилка: {r.get('Морозилка','')} | {r.get('Примечание','')}" for r in today_rows if r.get('Продукт')])
        resp = claude.messages.create(
            model="claude-haiku-4-5", max_tokens=1000,
            system="Ты аналитик кафе. Составь утреннюю сводку на русском.\nФормат:\n☀️ Доброе утро! Сводка по кофейне [дата]\n🔴 ЗАКОНЧИЛОСЬ / КРИТИЧНО:\n- список\n🟡 МАЛО ОСТАЛОСЬ (1-2 шт):\n- список\n💰 РАСХОДЫ ВЧЕРА: X THB\n📊 РАСХОДЫ ЗА МЕСЯЦ: X THB\n💡 РЕКОМЕНДАЦИИ:\n- 2-3 совета",
            messages=[{"role": "user", "content": f"Дата: {date_today}\nОстатки:\n{остатки_текст}\nРасходы вчера: {total_yesterday} THB\nРасходы последние: {total_recent} THB"}]
        )
        notify_owner(client_cfg, resp.content[0].text.strip())
    except Exception as e:
        print(f"Morning report error: {e}")

# ── Webhook ──────────────────────────────────────────────────────
@app.route("/webhook", methods=['POST'])
def webhook():
    body = request.get_data(as_text=True)
    try:
        events = json.loads(body)
    except:
        abort(400)
    destination = events.get('destination', '')
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
    for event in events.get('events', []):
        if event.get('type') != 'message':
            continue
        source = event.get('source', {})
        if source.get('type') not in ('group', 'room'):
            _msg = event.get('message', {})
            if _msg.get('type') == 'text' and _msg.get('text','').lower().strip() in ['сводка','отчет','отчёт','report']:
                morning_report(client_cfg)
            continue
        msg = event.get('message', {})
        msg_type = msg.get('type')
        date_only = datetime.datetime.now().strftime("%Y-%m-%d")
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        api = get_line_api(client_cfg['channel_access_token'])

        # ── Текст ──
        if msg_type == 'text':
            text = msg.get('text', '').strip()
            result = analyze_text(text, client_cfg)
            if result == 'IGNORE' or not result:
                continue
            if result.startswith('ВАЖНО [ПРОБЛЕМА]'):
                save_проблемы(client_cfg['sheet_id'], text, result, now_str)
                notify_owner(client_cfg, result)
                continue
            try:
                json_match = re.search(r'\{.*\}', result, re.DOTALL)
                if not json_match:
                    continue
                data = json.loads(json_match.group())
                if data['type'] == 'purchase':
                    save_закупки(client_cfg['sheet_id'], data['items'], date_only)
                    msg_text = "🛒 ЗАКУПКА записана:\n"
                    for item in data['items']:
                        msg_text += f"- {item['product']}: {item['quantity']}\n"
                    notify_owner(client_cfg, msg_text)
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
                        ws.append_row([date_only, 'Закупка', supplier, positions, total, ''])
                    notify_owner(client_cfg, f"💸 РАСХОД записан:\nМагазин: {supplier}\nПозиции: {positions}\nИтого: {total} THB")
            except Exception as e:
                print(f"Text handler error: {e}")

        # ── Фото ──
        elif msg_type == 'image':
            print(f'Image received, processing...')
            try:
                content = api.get_message_content(msg.get('id'))
                image_data = base64.b64encode(content.content).decode('utf-8')
                result = analyze_image(image_data, client_cfg)
                print(f'Image result: {result[:200]}')
                if 'NOT_FINANCE' in result:
                    continue
                json_match = re.search(r'\{.*\}', result, re.DOTALL)
                if not json_match:
                    continue
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
                print(f"Image handler error: {e}")
    return 'OK'

@app.route("/health", methods=['GET'])
def health():
    return {"status": "ok", "clients": len(CLIENTS), "sheets": SHEETS_ENABLED}, 200

# ── Планировщик ──────────────────────────────────────────────────
try:
    scheduler = BackgroundScheduler(timezone=pytz.timezone('Asia/Bangkok'))
    for bot_id, cfg in CLIENTS.items():
        if cfg.get('sheet_id') and gc:
            scheduler.add_job(morning_report, 'cron', hour=9, minute=0, args=[cfg], id=f"morning_{bot_id}")
    scheduler.start()
    print("Scheduler started")
except Exception as e:
    print(f"Scheduler error: {e}")

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)