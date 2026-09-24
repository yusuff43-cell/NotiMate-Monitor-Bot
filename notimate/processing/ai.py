"""OpenAI Responses API access: bounded retries, usage accounting, owner-facing prompts."""

from __future__ import annotations

import time

from openai import APIStatusError, RateLimitError

import app
from logging_utils import get_logger
from notimate.tenants import client_prompt_context

logger = get_logger()


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
            response = app.openai_client.responses.create(
                model=app.OPENAI_MODEL,
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
                    'model': app.OPENAI_MODEL,
                    'input_tokens': input_tokens,
                    'output_tokens': output_tokens,
                    'reasoning_tokens': reasoning_tokens,
                },
            )
            if app.event_store and app.DB_ENABLED:
                try:
                    app.event_store.record_openai_usage(
                        app.OPENAI_MODEL, input_tokens, output_tokens, reasoning_tokens
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


CAFE_GLOSSARY = "СЛОВАРЬ: Clear/Clear croissant=Масляный круассан, Chocolate=Шоколадный круассан, Almond=Миндальный круассан, Ham Cheese=Круассан с ветчиной и сыром, Cheesecake=Чизкейк, Biscoff cheesecake=Бискофф чизкейк, Cheese pancakes=Сырники, Mango cheese pancakes=Манговые сырники, Cucumber cheese pancakes=Огуречные сырники, Crepes=Шпинатные блинчики, Pancakes=Панкейки, Crepes burger=Блины для бургера, Pannacotta=Панна-котта, Chocolate mousse=Шоколадный мусс, Salted Caramel=Солёная карамель, Bounty=Баунти, Halva=Халва, Marzipan=Марципан, Brownie=Брауни, Banana bread=Банановый хлеб, Muffin=Маффин, Snickers=Сникерс, Napoleons=Наполеон, Sourdough=Хлеб на закваске (для брускет), Banana=Банан (не банановый хлеб), Coconut velvet=Кокосовое молоко велюр, Coconut milk velvet=Кокосовое молоко велюр, Dragon fruit=Драгон фрут, Salmon=Лосось, Yogurt=Йогурт, Açaí=Асаи"


def document_input_part(data_b64, mime='image/jpeg'):
    """One Responses API content part for a photo or a PDF (base64, no data: prefix)."""
    if mime == 'application/pdf':
        return {"type": "input_file", "filename": "document.pdf", "file_data": f"data:application/pdf;base64,{data_b64}"}
    return {"type": "input_image", "image_url": f"data:{mime};base64,{data_b64}", "detail": "high"}


def currency_hint(client_cfg):
    """Extra prompt line for tenants that don't use Thai baht (THB tenants get no change)."""
    currency = str(client_cfg.get('currency') or 'THB')
    if currency == 'THB':
        return ''
    symbols = {'KZT': '₸, тг, тенге', 'RUB': '₽, руб', 'USD': '$', 'EUR': '€'}.get(currency, currency)
    return f"\nВАЛЮТА КЛИЕНТА: {currency} ({symbols}). Все правила выше про символ ฿ и суммы применяй к этой валюте; в поле unit_price/amount возвращай только число.\n"


def analyze_image(image_data, client_cfg, mime='image/jpeg'):
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
Если не финансовый документ — верни: NOT_FINANCE{currency_hint(client_cfg)}""",
        [{"role": "user", "content": [
            {"type": "input_text", "text": "Проанализируй документ."},
            document_input_part(image_data, mime),
        ]}],
        2000,
    )


def glossary_block(client_cfg):
    """The café pastry glossary is JSC-specific vocabulary: it stays for every tenant that
    doesn't opt out with ``glossary: 'none'`` (existing LINE clients are unaffected), and is
    omitted for new tenants of other businesses so it can't bias their classification."""
    return '' if client_cfg.get('glossary') == 'none' else CAFE_GLOSSARY + '\n'


def analyze_text(text, client_cfg):
    business_context = client_prompt_context(client_cfg)
    return ask_openai(
        f"""КРИТИЧЕСКИ ВАЖНО: Только анализируй сообщения по правилам. Если не подходит — верни ТОЛЬКО: IGNORE
Входное сообщение может быть на русском, тайском или английском языке. Понимай все три языка. Названия товаров, описание проблемы, перевод, рекомендации и весь текст для владельца возвращай только на русском языке. JSON-ключи сохраняй точно по схеме.
КОНТЕКСТ КЛИЕНТА: {business_context}
{currency_hint(client_cfg)}
{glossary_block(client_cfg)}

ТИПЫ СООБЩЕНИЙ:

1. ЗАКУПКИ - сообщения со списком продуктов для заказа. Триггеры в начале: "we need", "for tomorrow", "need", "order". ИЛИ просто список продуктов с количествами без цен (каждая строка = продукт + количество).
Если есть сумма (฿, =число฿) — это РАСХОД (тип 4), не закупка
Верни ТОЛЬКО JSON: {{"type":"purchase","items":[{{"product":"название на русском","quantity":"только цифра без единиц измерения"}}]}}

2. ОСТАТКИ - начинаются с "Update"
Верни ТОЛЬКО JSON: {{"type":"stock","items":[{{"category":"Круассаны/Десерты/Блины и сырники/Макаруны/Начинки/Другое","product":"название на русском","fridge":"","freezer":"","note":""}}]}}
Правила note: "Out of stock" если всё 0, "Low stock" если 1-2 шт, "Exp today" если помечено

3. ОСТАТОК ОДНОЙ ПОЗИЦИИ - только если сообщение ЯВНО говорит об остатке: "товар have/has/left количество", "остаток", "เหลือ", "update" или количество с единицей измерения веса/объёма (г, кг, мл, л, pcs, банки). БЕЗ цены (฿). Если есть ฿ — это РАСХОД (тип 4)
ВАЖНО: простое сообщение "товар число" (например "avocado 5") без слов об остатке, без единиц измерения и без цены — это ЗАКУПКА (тип 1), а не остаток.
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
