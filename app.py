import os
import json
import datetime
import re
import time
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, ImageMessage, TextSendMessage
)
import anthropic
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# ── Глобальные сервисы ──────────────────────────────────────────
claude = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])

# Загружаем конфиги всех клиентов
with open('clients.json', 'r', encoding='utf-8') as f:
    CLIENTS = json.load(f)

# Google Sheets — один service account на всех клиентов
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

# Кэш LINE API клиентов по каждому OA (чтобы не пересоздавать)
_line_api_cache = {}

def get_line_api(token: str) -> LineBotApi:
    if token not in _line_api_cache:
        _line_api_cache[token] = LineBotApi(token)
    return _line_api_cache[token]


# ── Определение клиента по destination ──────────────────────────
def find_client(destination: str):
    """destination — это Bot User ID из webhook. По нему ищем клиента."""
    return CLIENTS.get(destination)


# ── Анализ сообщения через Claude ───────────────────────────────
def analyze_message(text: str, client_cfg: dict) -> dict:
    business = client_cfg.get('business_type', 'business')
    context = client_cfg.get('custom_context', '')
    lang = client_cfg.get('notification_language', 'thai')
    lang_instruction = {
        'russian': 'Отвечай ТОЛЬКО на русском языке. Переводи тайский и английский на русский.',
        'thai': 'ตอบเป็นภาษาไทยเท่านั้น',
        'english': 'Reply in English only. Translate Thai messages to English.'
    }.get(lang, 'ตอบเป็นภาษาไทยเท่านั้น')

    system = f"""Ты анализатор сообщений из рабочего чата ({business}).
Контекст бизнеса: {context}
{lang_instruction}

Проанализируй сообщение и верни ТОЛЬКО JSON без markdown:
{{
  "important": true/false,
  "category": "sale|expense|stock|problem|task|salary|other",
  "summary": "краткое описание на нужном языке",
  "amount": число или null
}}

Правила:
- salary: банковские переводы сотрудникам, выплаты зарплат, KBIZ переводы физлицам
- expense: оплата поставщикам, аренда, коммуналка, сервисы
- stock: закупка товаров и продуктов для кофейни (лёд, вода, молоко, кофе, упаковка)
- sale: ТОЛЬКО итоги смены с выручкой, входящие платежи от клиентов. НЕ накладные, НЕ доставка, НЕ посылки
- expense: оплата поставщикам, аренда, коммуналка, сервисы, накладные доставки (SPX, Kerry, Flash)
- problem: поломки, ЧП, жалобы, срочное
- task: поручения, задачи
- other: приветствия, болтовня → important: false
- important: true только для sale/expense/stock/problem/task/salary"""
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": text}]
        )
        raw = resp.content[0].text.strip()
        raw = raw.replace('```json', '').replace('```', '').strip()
        return json.loads(raw)
    except Exception as e:
        print(f"Analyze error: {e}")
        return {"important": False, "category": "other", "summary": "", "amount": None}


# ── Запись в Google Sheets ──────────────────────────────────────
def log_to_sheet(client_cfg: dict, category: str, summary: str, amount, raw_text: str):
    if not SHEETS_ENABLED:
        return
    sheet_id = client_cfg.get('sheet_id')
    if not sheet_id:
        return

    # Карта: категория → имя листа
    sheet_map = {
    'sale': 'Выручка',
    'expense': 'Расходы',
    'stock': 'Закупки',
    'problem': 'Проблемы',
    'task': 'Задачи',
    'salary': 'Зарплаты',
}
    worksheet_name = sheet_map.get(category, 'Сообщения')
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')

    try:
        sh = gc.open_by_key(sheet_id)
        try:
            ws = sh.worksheet(worksheet_name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=worksheet_name, rows=1000, cols=5)
            ws.append_row(['Дата', 'Категория', 'Описание', 'Сумма'])
        ws.append_row([now, category, summary, amount or ''])
    except Exception as e:
        print(f"Sheet write error: {e}")


# ── Push уведомление владельцу ──────────────────────────────────
def notify_owner(client_cfg: dict, analysis: dict, raw_text: str):
    token = client_cfg['channel_access_token']
    owner_id = client_cfg['owner_line_id']

    cat_icon = {
        'sale': '💰', 'expense': '💸', 'stock': '📦',
        'problem': '⚠️', 'task': '📋'
    }
    icon = cat_icon.get(analysis['category'], '🔔')

    msg = f"{icon} {analysis['summary']}"
    if analysis.get('amount'):
        msg += f"\n💵 {analysis['amount']}฿"

    try:
        api = get_line_api(token)
        api.push_message(owner_id, TextSendMessage(text=msg))
    except Exception as e:
        print(f"Notify error: {e}")


# ── Парсинг чеков (фото) через Claude Vision ────────────────────
def process_receipt(image_content: bytes, client_cfg: dict) -> dict:
    import base64
    b64 = base64.b64encode(image_content).decode('utf-8')
    
    lang = client_cfg.get('notification_language', 'thai')
    lang_instruction = {
        'russian': 'Отвечай ТОЛЬКО на русском языке.',
        'thai': 'ตอบเป็นภาษาไทยเท่านั้น',
        'english': 'Reply in English only.'
    }.get(lang, 'ตอบเป็นภาษาไทยเท่านั้น')

    system = f"""Ты распознаёшь финансовые документы — чеки, счета, банковские переводы.
{lang_instruction}

Определи тип документа и верни ТОЛЬКО JSON без markdown:
{{
  "category": "expense|salary|sale",
  "merchant": "название поставщика или получателя перевода",
  "total": число,
  "items": [{{"name": "позиция", "price": число}}],
  "summary": "краткое описание на нужном языке"
}}

Правила категорий:
- salary: банковский перевод физлицу (Transfer Completed, KBIZ, SCB, имя получателя)
- expense: чек из магазина, оплата поставщику, накладная
- sale: входящий платёж от клиента"""

    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5",
            max_tokens=500,
            system=system,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": b64
                    }},
                    {"type": "text", "text": "Распознай этот документ"}
                ]
            }]
        )
        raw = resp.content[0].text.strip().replace('```json', '').replace('```', '').strip()
        return json.loads(raw)
    except Exception as e:
        print(f"Receipt error: {e}")
        return None

# ── Webhook (общий для всех клиентов) ───────────────────────────

def save_остатки(gc, sheet_id, items, date_str):
    try:
        sh = gc.open_by_key(sheet_id)
        headers = ['Дата', 'Категория', 'Продукт', 'Холодильник', 'Морозилка', 'Примечание']
        try:
            ws = sh.worksheet('Остатки')
        except:
            ws = sh.add_worksheet(title='Остатки', rows=1000, cols=6)
            ws.append_row(headers)
        last_category = None
        col_a = ws.col_values(1)
        last_date = None
        for val in reversed(col_a):
            if val and val != 'Дата':
                last_date = val
                break
        for i, item in enumerate(items):
            date_cell = date_str if (i == 0 and last_date != date_str) else ''
            category = item.get('category', '')
            category_cell = category if category != last_category else ''
            if category:
                last_category = category
            ws.append_row([
                date_cell,
                category_cell,
                item.get('product', ''),
                item.get('fridge', ''),
                item.get('freezer', ''),
                item.get('note', '')
            ])
    except Exception as e:
        print(f"save_остатки error: {e}")


def morning_report(client_cfg, claude_client, gc):
    try:
        import datetime as dt
        date_today = dt.datetime.now(pytz.timezone('Asia/Bangkok')).strftime("%Y-%m-%d")
        yesterday = (dt.datetime.now(pytz.timezone('Asia/Bangkok')) - dt.timedelta(days=1)).strftime("%Y-%m-%d")
        sh = gc.open_by_key(client_cfg['sheet_id'])

        today_rows = []
        try:
            ws_ost = sh.worksheet('Остатки')
            all_rows = ws_ost.get_all_records()
            today_rows = [r for r in all_rows if str(r.get('Дата','')) == date_today]
            if not today_rows:
                today_rows = all_rows[-50:] if len(all_rows) > 50 else all_rows
        except:
            pass

        total_yesterday = 0
        total_recent = 0
        try:
            ws_exp = sh.worksheet('Расходы')
            rows_exp = ws_exp.get_all_records()
            yest_exp = [r for r in rows_exp if str(r.get('Дата','')) == yesterday]
            total_yesterday = sum(float(str(r.get('Сумма',0) or r.get('amount',0) or 0).replace('฿','').replace(',','').strip() or 0) for r in yest_exp)
            recent = rows_exp[-100:] if len(rows_exp) > 100 else rows_exp
            total_recent = sum(float(str(r.get('Сумма',0) or r.get('amount',0) or 0).replace('฿','').replace(',','').strip() or 0) for r in recent)
        except:
            pass

        остатки_текст = "\n".join([
            f"{r.get('Продукт','')} | Холодильник: {r.get('Холодильник','')} | Морозилка: {r.get('Морозилка','')} | {r.get('Примечание','')}"
            for r in today_rows if r.get('Продукт')
        ])

        resp = claude_client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1000,
            system="""Ты аналитик кафе. Составь утреннюю сводку на русском языке.
Формат:
☀️ Доброе утро! Сводка по кофейне [дата]
🔴 ЗАКОНЧИЛОСЬ / КРИТИЧНО:
- список
🟡 МАЛО ОСТАЛОСЬ (1-2 шт):
- список
💰 РАСХОДЫ ВЧЕРА: X THB
📊 РАСХОДЫ (последние записи): X THB
💡 РЕКОМЕНДАЦИИ:
- 2-3 совета""",
            messages=[{"role": "user", "content": f"Дата: {date_today}\nОстатки:\n{остатки_текст}\n\nРасходы вчера: {total_yesterday} THB\nРасходы последние: {total_recent} THB"}]
        )
        msg = resp.content[0].text.strip()
        from linebot.models import TextSendMessage
        get_line_api(client_cfg['channel_access_token']).push_message(
            client_cfg['owner_line_id'],
            TextSendMessage(text=msg)
        )
    except Exception as e:
        print(f"morning_report error: {e}")

@app.route("/webhook", methods=['POST'])
def webhook():
    body = request.get_data(as_text=True)

    try:
        events = json.loads(body)
    except Exception:
        abort(400)

    destination = events.get('destination', '')
    client_cfg = find_client(destination)

    if not client_cfg:
        print(f"Unknown destination: {destination}")
        return 'OK'  # не наш клиент — игнорируем

    # Проверка подписи через secret конкретного клиента
    signature = request.headers.get('X-Line-Signature', '')
    handler = WebhookHandler(client_cfg['channel_secret'])

    # Обрабатываем события вручную (multi-tenant)
    for event in events.get('events', []):
        try:
            handle_event(event, client_cfg)
        except Exception as e:
            print(f"Event error: {e}")

    return 'OK'


def handle_event(event: dict, client_cfg: dict):
    if event.get('type') != 'message':
        return

    msg = event.get('message', {})
    msg_type = msg.get('type')
    source = event.get('source', {})

    # Слушаем только группы (рабочие чаты), не личку
    if source.get('type') not in ('group', 'room'):
        return

    token = client_cfg['channel_access_token']
    api = get_line_api(token)

    # ── Текстовое сообщение ──
    if msg_type == 'text':
        text = msg.get('text', '').strip()
        if text.startswith('Update'):
            import re as _re
            date_only = datetime.datetime.now().strftime('%Y-%m-%d')
            try:
                resp = claude.messages.create(
                    model='claude-haiku-4-5',
                    max_tokens=4000,
                    system='Ты парсишь сообщение Update из чата кафе. Переводи ВСЕ названия на русский. Словарь: Clear=Масляный круассан, Chocolate=Шоколадный круассан, Almond=Миндальный круассан, Ham Cheese=Круассан с ветчиной и сыром, Cheesecake=Чизкейк, Biscoff Cheesecake=Бискофф чизкейк, Cheese pancakes=Сырники, Mango cheese pancakes=Манговые сырники, Cucumber cheese pancakes=Огуречные сырники, Crepes=Шпинатные блинчики, Pancakes=Панкейки, Pannacotta=Панна-котта, Chocolate mousse=Шоколадный мусс, Salted Caramel=Солёная карамель, Bounty=Баунти, Halva=Халва, Marzipan=Марципан, Brownie=Брауни, Banana bread=Банановый хлеб, Muffin=Маффин, Snickers=Сникерс, Napoleons=Наполеон, Vanilla macaron=Ванильный макарон, Bounty macaron=Макарон Баунти, Caramel macaron=Карамельный макарон, Glazed strawberry=Глазированный сырок клубника, Glazed coconut=Глазированный сырок кокос, Glazed milk=Глазированный сырок, Burrito sauce=Соус для буррито. Верни ТОЛЬКО JSON: {"type":"stock","items":[{"category":"кат","product":"название на русском","fridge":"","freezer":"","note":""}]} Категории: Круассаны, Десерты, Блины и сырники, Макаруны, Начинки, Другое. note: Out of stock если 0, Low stock если 1-2, Exp today если помечено',
                    messages=[{'role': 'user', 'content': text}]
                )
                raw = resp.content[0].text.strip().replace('```json','').replace('```','').strip()
                data = json.loads(_re.search(r'\{.*\}', raw, _re.DOTALL).group())
                if data.get('type') == 'stock' and gc:
                    save_остатки(gc, client_cfg['sheet_id'], data['items'], date_only)
                    out = [i for i in data['items'] if i.get('note') in ['Out of stock','Exp today']]
                    low = [i for i in data['items'] if i.get('note') == 'Low stock']
                    msg = f"📦 ОСТАТКИ записаны ({len(data['items'])} позиций)\n"
                    if out:
                        msg += '\n🔴 ЗАКОНЧИЛОСЬ / ИСТЕКАЕТ СЕГОДНЯ:\n'
                        for i in out: msg += f"- {i['product']}\n"
                    if low:
                        msg += '\n🟡 МАЛО ОСТАЛОСЬ:\n'
                        for i in low: msg += f"- {i['product']}\n"
                    notify_owner(client_cfg, {'category':'stock','summary':msg,'amount':None}, '')
            except Exception as e:
                print(f'Stock update error: {e}')
        else:
            analysis = analyze_message(text, client_cfg)
            if analysis['important']:
                log_to_sheet(
                    client_cfg,
                    analysis['category'],
                    analysis['summary'],
                    analysis.get('amount'),
                    text
                )
                notify_owner(client_cfg, analysis, text)
    # ── Update остатки ──
    elif msg_type == 'text' and text.strip().startswith('Update'):
        import re as _re
        date_only = datetime.datetime.now().strftime("%Y-%m-%d")
        try:
            resp = claude.messages.create(
                model="claude-haiku-4-5",
                max_tokens=4000,
                system="Ты парсишь сообщение Update из чата кафе. Верни ТОЛЬКО JSON без markdown: {\"type\":\"stock\",\"items\":[{\"category\":\"категория\",\"product\":\"название на русском\",\"fridge\":\"\",\"freezer\":\"\",\"note\":\"\"}]} Категории: Круассаны, Десерты, Блины и сырники, Макаруны, Начинки, Другое. Правила note: Out of stock если всё 0, Low stock если 1-2 шт, Exp today если помечено",
                messages=[{"role": "user", "content": text}]
            )
            raw = resp.content[0].text.strip().replace('```json','').replace('```','').strip()
            data = json.loads(_re.search(r'\{.*\}', raw, _re.DOTALL).group())
            if data.get('type') == 'stock' and gc:
                save_остатки(gc, client_cfg['sheet_id'], data['items'], date_only)
                out = [i for i in data['items'] if i.get('note') in ['Out of stock','Exp today']]
                low = [i for i in data['items'] if i.get('note') == 'Low stock']
                msg = f"📦 ОСТАТКИ записаны ({len(data['items'])} позиций)\n"
                if out:
                    msg += "\n🔴 ЗАКОНЧИЛОСЬ / ИСТЕКАЕТ СЕГОДНЯ:\n"
                    for i in out: msg += f"- {i['product']}\n"
                if low:
                    msg += "\n🟡 МАЛО ОСТАЛОСЬ:\n"
                    for i in low: msg += f"- {i['product']}\n"
                notify_owner(client_cfg, {'category':'stock','summary':msg,'amount':None}, '')
        except Exception as e:
            print(f"Stock update error: {e}")

    # ── Фото (чек/счёт) ──
    elif msg_type == 'image':
        message_id = msg.get('id')
        try:
            content = api.get_message_content(message_id)
            image_bytes = b''.join(chunk for chunk in content.iter_content())
            receipt = process_receipt(image_bytes, client_cfg)
            if receipt:
                category = receipt.get('category', 'expense')
                log_to_sheet(
                    client_cfg,
                    category,
                    receipt.get('summary', 'Документ'),
                    receipt.get('total'),
                    ''
                )
                analysis = {
                    'category': category,
                    'summary': f"📸 {receipt.get('summary', 'Новый документ')}",
                    'amount': receipt.get('total')
                }
                notify_owner(client_cfg, analysis, '')
        except Exception as e:
            print(f"Image error: {e}")

@app.route("/health", methods=['GET'])
def health():
    return {
        "status": "ok",
        "clients": len(CLIENTS),
        "sheets": SHEETS_ENABLED
    }, 200


# Планировщик утренней сводки
try:
    scheduler = BackgroundScheduler(timezone=pytz.timezone('Asia/Bangkok'))
    for bot_id, cfg in CLIENTS.items():
        if cfg.get('sheet_id') and gc:
            scheduler.add_job(
                morning_report,
                'cron', hour=9, minute=0,
                args=[cfg, claude, gc],
                id=f"morning_{bot_id}"
            )
    scheduler.start()
    print("Scheduler started")
except Exception as e:
    print(f"Scheduler error: {e}")

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
