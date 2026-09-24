"""Operator (developer/support) commands over WhatsApp.

The operator manages access from the same chat, no console needed. Numbers come from
``OPERATOR_WHATSAPP_IDS``. Commands (Russian, case-insensitive):

  заявки                                   pending access requests
  клиенты                                  businesses and their ids
  одобрить <№> <id клиента> [роль] [точка:<id>] [имя…]     role: сотрудник|владелец|бухгалтер
  отклонить <№>

Because the operator writes to the bot, replies land inside WhatsApp's 24-hour window — this is
also the reliable way to read requests, since a bot-initiated notification outside the window
would need an approved template (TASK-055).
"""

from __future__ import annotations

import os
from typing import Any

ROLE_WORDS = {'сотрудник': 'staff', 'staff': 'staff', 'владелец': 'owner', 'owner': 'owner', 'бухгалтер': 'accountant', 'accountant': 'accountant'}
ROLE_LABEL = {'staff': 'сотрудник', 'owner': 'владелец', 'accountant': 'бухгалтер'}
HELP = ('Команды оператора:\nзаявки — новые заявки на доступ\nклиенты — список бизнесов и их id\n'
        'одобрить <№> <id клиента> [сотрудник|владелец|бухгалтер] [точка:<id>] [имя] — открыть доступ\nотклонить <№>')


def operator_ids() -> set[str]:
    return {o.strip() for o in os.environ.get('OPERATOR_WHATSAPP_IDS', '').split(',') if o.strip()}


def is_operator(sender_id: str) -> bool:
    return sender_id in operator_ids()


def handle_operator_command(phone_number_id: str, config: dict[str, Any], sender_id: str, text: str) -> bool:
    """Handle one operator message; returns True when it was an operator command."""
    import app
    from notimate.access import apply_approval

    parts = text.strip().lstrip('/').split()
    if not parts:
        return False
    word = parts[0].lower()
    if word not in ('заявки', 'клиенты', 'одобрить', 'отклонить', 'оператор'):
        return False

    def reply(body: str) -> None:
        app.whatsapp_send_text(config['access_token'], phone_number_id, sender_id, body)

    tenants, access = app.tenant_store, getattr(app, 'access_store', None)
    if word == 'оператор':
        reply(HELP)
    elif word == 'клиенты':
        rows = tenants.list_tenants()
        reply('\n'.join(f"• {t['id']} — {t['name']} ({t.get('vertical_pack') or 'без режима'})" for t in rows) or 'Клиентов нет.')
    elif word == 'заявки':
        pending = access.list_pending() if access else []
        if not pending:
            reply('Новых заявок нет.')
        else:
            reply('\n'.join(f"#{r['id']} +{r['sender_id']}: «{(r['message'] or '')[:120]}»" for r in pending[:15])
                  + '\n\nОдобрить: одобрить <№> <id клиента> [роль]')
    elif word == 'отклонить':
        if len(parts) < 2 or not parts[1].isdigit() or not access:
            reply('Формат: отклонить <№>')
            return True
        request = access.get(int(parts[1]))
        if not request or request['status'] != 'pending':
            reply('Заявка не найдена или уже обработана.')
            return True
        access.decide(request['id'], 'rejected')
        app.whatsapp_send_text(config['access_token'], phone_number_id, request['sender_id'], 'К сожалению, доступ не подтверждён. Если это ошибка — свяжитесь с владельцем бизнеса.')
        reply(f"Заявка #{request['id']} отклонена.")
    else:  # одобрить
        if len(parts) < 3 or not parts[1].isdigit() or not access:
            reply('Формат: одобрить <№> <id клиента> [сотрудник|владелец|бухгалтер] [точка:<id>] [имя]')
            return True
        request = access.get(int(parts[1]))
        if not request or request['status'] != 'pending':
            reply('Заявка не найдена или уже обработана.')
            return True
        tenant_id, rest = parts[2], parts[3:]
        role, location, name_parts = 'staff', None, []
        for token in rest:
            if token.lower() in ROLE_WORDS and not name_parts:
                role = ROLE_WORDS[token.lower()]
            elif token.lower().startswith('точка:'):
                location = token.split(':', 1)[1]
            else:
                name_parts.append(token)
        try:
            result = apply_approval(tenants, app.location_reports_store, request, role, location_id=location, name=' '.join(name_parts), tenant_id=tenant_id)
        except ValueError as exc:
            reply(f'Не удалось: {exc}')
            return True
        access.decide(request['id'], 'approved', result['tenant_id'], role)
        app.whatsapp_send_text(config['access_token'], phone_number_id, request['sender_id'],
                               f"✅ Доступ открыт: {result['tenant_name']}, роль — {ROLE_LABEL[role]}. Напишите «помощь», чтобы увидеть, что умеет бот.")
        reply(f"Готово: +{request['sender_id']} → {result['tenant_name']} ({ROLE_LABEL[role]}).")
    return True
