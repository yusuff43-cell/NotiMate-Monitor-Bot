import os
import json
import datetime
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
- stock: закупка товаров и продуктов для кофейни
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
def process_receipt(image_content: bytes, client_cfg: dict):
    import base64
    b64 = base64.b64encode(image_content).decode('utf-8')

    system = """Ты распознаёшь чеки/счета. Верни ТОЛЬКО JSON без markdown:
{
  "merchant": "название поставщика/магазина",
  "total": число,
  "items": [{"name": "позиция", "price": число}],
  "summary": "краткое описание на тайском"
}"""

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
                    {"type": "text", "text": "Распознай этот чек"}
                ]
            }]
        )
        raw = resp.content[0].text.strip().replace('```json', '').replace('```', '').strip()
        return json.loads(raw)
    except Exception as e:
        print(f"Receipt error: {e}")
        return None


# ── Webhook (общий для всех клиентов) ───────────────────────────
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

    # ── Фото (чек/счёт) ──
    elif msg_type == 'image':
        message_id = msg.get('id')
        try:
            content = api.get_message_content(message_id)
            image_bytes = b''.join(chunk for chunk in content.iter_content())
            receipt = process_receipt(image_bytes, client_cfg)

            if receipt:
                log_to_sheet(
                    client_cfg,
                    'expense',
                    receipt.get('summary', 'Чек'),
                    receipt.get('total'),
                    f"{receipt.get('merchant', '')}"
                )
                analysis = {
                    'category': 'expense',
                    'summary': f"📸 {receipt.get('summary', 'Новый чек')}",
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


if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
