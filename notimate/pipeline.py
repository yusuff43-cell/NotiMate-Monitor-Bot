"""Dispatch one durable event claimed by the worker to AI, projections and the owner."""

from __future__ import annotations

import base64
import json
import re

import app
from logging_utils import get_logger
from notimate.inbound import build_whatsapp_inbound_message
from notimate.projections.sheets import _money
from notimate.tenants import is_group_allowed
from notimate.timeutil import bangkok_now

logger = get_logger()


def _record_expense_items(tenant_id, event_id, effect, items, supplier, occurred_on):
    """Dual-write one operations row per expense item — mirrors save_расходы's one-Sheets-row-per-item loop."""
    for index, item in enumerate(items):
        app.record_operation_safely(
            tenant_id, f'{event_id}:{effect}:{index}', 'expense', occurred_on,
            _money(item.get('amount')), 'THB', item.get('supplier', supplier) or supplier,
            item.get('description', ''),
        )


def process_line_event(destination, event):
    """Process one event claimed by the durable worker."""
    client_cfg = app.find_client(destination)
    if not client_cfg:
        raise ValueError(f"Unknown destination: {destination}")
    if event.get('type') != 'message':
        return
    source = event.get('source', {})
    if source.get('type') in ('group', 'room') and not is_group_allowed(client_cfg, source):
        # Content-free: nothing from this group reaches OpenAI, Sheets or the owner.
        logger.info('event_skipped_group_not_allowed', extra={'event_id': event.get('webhookEventId')})
        return
    if not app.SHEETS_ENABLED:
        raise RuntimeError('Google Sheets is not ready')
    if source.get('type') not in ('group', 'room'):
        allowed_owners = {client_cfg.get('owner_line_id'), client_cfg.get('owner_line_id_2')}
        if source.get('userId') not in allowed_owners:
            return
        _msg = event.get('message', {})
        command = _msg.get('text', '').lower().strip() if _msg.get('type') == 'text' else ''
        if command in ['сводка', 'отчет', 'отчёт', 'подробный отчёт', 'report']:
            app.detailed_report(client_cfg)
        elif command in ['деньги', 'финансы', 'money']:
            app.evening_summary(client_cfg)
        elif command in ['напоминания', 'reminders']:
            app.reminders_report(client_cfg)
        elif command in ['меню', 'menu']:
            app.owner_menu(client_cfg)
        elif command in ['неделя', 'week', 'недельная']:
            app.weekly_report(client_cfg)
        return
    msg = event.get('message', {})
    msg_type = msg.get('type')
    event_id = event.get('webhookEventId')
    current_bangkok = bangkok_now()
    date_only = current_bangkok.strftime("%Y-%m-%d")
    now_str = current_bangkok.strftime("%Y-%m-%d %H:%M")
    blob_api = app.get_line_blob_api(client_cfg['channel_access_token'])

    # ── Текст ──
    if msg_type == 'text':
        text = msg.get('text', '').strip()
        result = app.analyze_text(text, client_cfg)
        if result == 'IGNORE' or not result:
            return
        if result.startswith('ВАЖНО [ПРОБЛЕМА]'):
            app.save_проблемы(client_cfg['sheet_id'], text, result, now_str, event_id)
            app.record_issue_safely(destination, f'{event_id}:problem:0', date_only, text, result)
            app.refresh_overview_safely(client_cfg)
            app.notify_owner(client_cfg, result)
            return
        try:
            json_match = re.search(r'\{.*\}', result, re.DOTALL)
            if not json_match:
                return
            data = json.loads(json_match.group())
            if data['type'] == 'purchase':
                app.save_закупки(client_cfg['sheet_id'], data['items'], date_only, event_id)
                for index, item in enumerate(data['items']):
                    app.record_operation_safely(
                        destination, f'{event_id}:purchase:{index}', 'purchase', date_only,
                        None, 'THB', None, item.get('product', ''),
                        {'quantity': item.get('quantity', '')},
                    )
                app.refresh_overview_safely(client_cfg)
                msg_text = "🛒 ЗАКУПКА записана:\n"
                for item in data['items']:
                    msg_text += f"- {item['product']}: {item['quantity']}\n"
                # уведомление в дайджесте 18:00
            elif data['type'] == 'stock':
                app.save_остатки(client_cfg['sheet_id'], data['items'], date_only, event_id)
                for index, item in enumerate(data['items']):
                    app.record_stock_signal_safely(
                        destination, f'{event_id}:stock:{index}', date_only,
                        item.get('category', ''), item.get('product', ''),
                        item.get('fridge', ''), item.get('freezer', ''), item.get('note', ''),
                    )
                app.refresh_overview_safely(client_cfg)
                out = [i for i in data['items'] if i.get('note') in ['Out of stock','Exp today']]
                low = [i for i in data['items'] if i.get('note') == 'Low stock']
                msg_text = f"📦 ОСТАТКИ записаны ({len(data['items'])} позиций)\n"
                if out:
                    msg_text += "\n🔴 ЗАКОНЧИЛОСЬ / ИСТЕКАЕТ СЕГОДНЯ:\n"
                    for i in out: msg_text += f"- {i['product']}\n"
                if low:
                    msg_text += "\n🟡 МАЛО ОСТАЛОСЬ:\n"
                    for i in low: msg_text += f"- {i['product']}\n"
                app.notify_owner(client_cfg, msg_text)
            elif data['type'] == 'single_stock':
                app.save_одиночный_остаток(client_cfg['sheet_id'], data.get('product',''), data.get('amount',''), date_only, event_id)
                app.record_stock_signal_safely(
                    destination, f'{event_id}:single-stock:0', date_only,
                    '', data.get('product', ''), data.get('amount', ''), '', '',
                )
                app.refresh_overview_safely(client_cfg)
                app.notify_owner(client_cfg, f"📦 Остаток записан:\n{data.get('product','')}: {data.get('amount','')}")
            elif data['type'] == 'text_expense':
                items = data.get('items', [])
                total = data.get('total', '')
                supplier = data.get('supplier', '')
                positions = ', '.join([i['description'] for i in items if i.get('description')])
                app.save_расходы(client_cfg['sheet_id'], [{'type': 'Закупка', 'description': positions, 'amount': total}], date_only, supplier, event_id=event_id, effect='text-expense')
                app.record_operation_safely(destination, f'{event_id}:text-expense:0', 'expense', date_only, _money(total), 'THB', supplier, positions)
                app.refresh_overview_safely(client_cfg)
                app.notify_owner(client_cfg, f"💸 РАСХОД записан:\nМагазин: {supplier}\nПозиции: {positions}\nИтого: {total} THB")
        except Exception as e:
            raise RuntimeError(f"Text handler error: {e}") from e

    # ── Фото ──
    elif msg_type == 'image':
        logger.info('image_processing_started', extra={'event_id': event_id})
        try:
            content = blob_api.get_message_content(msg.get('id'))
            image_data = base64.b64encode(content).decode('utf-8')
            result = app.analyze_image(image_data, client_cfg)
            if 'NOT_FINANCE' in result:
                return
            json_match = re.search(r'\{.*\}', result, re.DOTALL)
            if not json_match:
                return
            data = json.loads(json_match.group())
            doc_type = data.get('doc_type')
            logger.info('image_analysis_completed', extra={'event_id': event_id, 'document_type': doc_type or 'unknown'})
            if doc_type == 'shift':
                app.save_выручка(client_cfg['sheet_id'], data, date_only, data.get('note',''), event_id)
                app.record_operation_safely(
                    destination, f'{event_id}:shift:0', 'revenue', date_only,
                    _money(data.get('gross_sales')), 'THB', None, f"Смена {data.get('shift','')}",
                    {'cash': data.get('cash'), 'card': data.get('card'), 'qr': data.get('qr'), 'difference': data.get('difference')},
                )
                app.refresh_overview_safely(client_cfg)
                diff = data.get('difference', 0)
                msg_text = f"💰 Смена #{data.get('shift','?')}\n"
                msg_text += f"📊 Выручка: {data.get('gross_sales','')} THB\n"
                msg_text += f"💵 Наличные: {data.get('cash','')} THB\n"
                msg_text += f"💳 Карта: {data.get('card','')} THB\n"
                msg_text += f"📱 QR: {data.get('qr','')} THB\n"
                msg_text += f"✅ Касса: {'+' if float(diff or 0) >= 0 else ''}{diff} THB"
                app.notify_owner(client_cfg, msg_text)
            elif doc_type == 'invoice':
                app.save_расходы(client_cfg['sheet_id'], data.get('items',[]), date_only, data.get('supplier',''), data.get('note',''), event_id, 'invoice-expense')
                _record_expense_items(destination, event_id, 'invoice-expense', data.get('items', []), data.get('supplier', ''), date_only)
                app.check_price_drift(client_cfg['sheet_id'], data.get('items',[]), data.get('supplier',''), client_cfg, event_id)
                app.refresh_overview_safely(client_cfg)
                app.notify_owner(client_cfg, f"🧾 НАКЛАДНАЯ записана\nПоставщик: {data.get('supplier','—')}\nИтого: {data.get('total','—')} THB")
            elif doc_type == 'expense':
                app.save_расходы(client_cfg['sheet_id'], data.get('items',[]), date_only, data.get('supplier',''), data.get('note',''), event_id, 'receipt-expense')
                _record_expense_items(destination, event_id, 'receipt-expense', data.get('items', []), data.get('supplier', ''), date_only)
                app.refresh_overview_safely(client_cfg)
                app.notify_owner(client_cfg, f"🛒 РАСХОД записан\nМагазин: {data.get('supplier','—')}\nИтого: {data.get('total','—')} THB")
            elif doc_type == 'salary':
                app.save_зарплаты(client_cfg['sheet_id'], [data], date_only, event_id)
                app.record_operation_safely(destination, f'{event_id}:salary:0', 'salary', date_only, _money(data.get('amount')), 'THB', data.get('recipient', ''), data.get('note', ''))
                app.refresh_overview_safely(client_cfg)
                app.notify_owner(client_cfg, f"💼 ЗАРПЛАТА записана\nПолучатель: {data.get('recipient','—')}\nСумма: {data.get('amount','—')} THB")
            elif doc_type == 'reminder':
                app.save_напоминание(client_cfg['sheet_id'], data, date_only, event_id)
                app.record_reminder_safely(destination, f'{event_id}:reminder:0', data.get('title', ''), data.get('expiry_date') or None, date_only, data.get('note', ''))
                app.refresh_overview_safely(client_cfg)
                app.notify_owner(client_cfg, f"📅 НАПОМИНАНИЕ записано\n📄 {data.get('title','—')}\n⏰ Истекает: {data.get('expiry_date','—')}")
            elif doc_type == 'notice':
                app.notify_owner(client_cfg, f"⚡️ ВАЖНОЕ УВЕДОМЛЕНИЕ\n\n{data.get('title','')}\n\n{data.get('content','')}")
            elif doc_type == 'bank_history':
                items = data.get('items', [])
                expenses = [i for i in items if i.get('type') == 'expense']
                salaries = [i for i in items if i.get('type') == 'salary']
                if expenses:
                    expense_rows = [{'type': 'Закупка', 'supplier': item.get('recipient',''), 'description': '', 'amount': item.get('amount',''), 'note': item.get('note','')} for item in expenses]
                    app.save_расходы(client_cfg['sheet_id'], expense_rows, date_only, '', event_id=event_id, effect='bank-expense')
                    _record_expense_items(destination, event_id, 'bank-expense', expense_rows, '', date_only)
                if salaries:
                    app.save_зарплаты(client_cfg['sheet_id'], salaries, date_only, event_id)
                    for index, item in enumerate(salaries):
                        app.record_operation_safely(
                            destination, f'{event_id}:salary:{index}', 'salary', date_only,
                            _money(item.get('amount')), 'THB', item.get('recipient', ''), item.get('note', ''),
                        )
                if expenses or salaries:
                    app.refresh_overview_safely(client_cfg)
                msg = f"🏦 ТРАНЗАКЦИИ записаны ({len(items)} шт)\n"
                if expenses:
                    msg += f"💸 Расходы: {len(expenses)} шт\n"
                if salaries:
                    msg += f"💼 Зарплаты: {len(salaries)} шт"
                app.notify_owner(client_cfg, msg)
        except Exception as e:
            raise RuntimeError(f"Image handler error: {e}") from e


def process_whatsapp_event(phone_number_id, message):
    """Process one inbound WhatsApp message claimed by the durable worker.

    Этап 3 MVP per docs/21: proves the adapter → inbound_events → worker → reply loop
    works end to end for a real WhatsApp number. Real business logic (drafts, «Отчёты
    точек») is Этап 6 — this only acknowledges receipt so far.
    """
    row = app.find_whatsapp_channel(phone_number_id)
    if not row:
        raise ValueError(f"Unknown WhatsApp phone_number_id: {phone_number_id}")
    config = app.whatsapp_channel_config(row, app.WHATSAPP_SECRETS.get(row['channel']['secret_ref']))
    if not config:
        raise RuntimeError('WhatsApp channel config is incomplete')
    inbound = build_whatsapp_inbound_message(row, message)
    if not inbound.text:
        return
    app.whatsapp_send_text(
        config['access_token'], config['phone_number_id'], inbound.sender_id,
        f"Получено: {inbound.text}",
    )
