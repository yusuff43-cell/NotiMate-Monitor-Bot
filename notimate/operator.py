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
COUNTRY_DEFAULTS = {
    'KZ': ('Asia/Almaty', 'KZT'), 'TH': ('Asia/Bangkok', 'THB'), 'RU': ('Europe/Moscow', 'RUB'),
}
PACKS = ('monitor', 'location_reports', 'accountant')
TENANT_ID_RE = r'[a-z0-9][a-z0-9-]{2,40}'
HELP = (
    'Команды оператора:\n'
    'заявки — новые заявки на доступ\n'
    'клиенты — список бизнесов и их id\n'
    'одобрить <№> <id клиента> [сотрудник|владелец|бухгалтер] [точка:<id>] [имя] — открыть доступ\n'
    'отклонить <№>\n'
    'создать <id> <KZ|TH|RU> <режим> <Название> — новый бизнес\n'
    'таблица <id> <ссылка> — привязать таблицу\n'
    'группа <id> <ключ> — объединить бизнесы одного владельца\n'
    'режим <id> <режим> [+доп] — сменить режим'
)


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
    if word not in ('заявки', 'клиенты', 'одобрить', 'отклонить', 'оператор', 'создать', 'таблица', 'группа', 'режим'):
        return False

    def reply(body: str) -> None:
        app.whatsapp_send_text(config['access_token'], phone_number_id, sender_id, body)

    tenants, access = app.tenant_store, getattr(app, 'access_store', None)
    if word == 'оператор':
        reply(HELP)
    elif word in ('создать', 'таблица', 'группа', 'режим'):
        _handle_setup_command(word, parts, tenants, reply)
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


def _service_account_email() -> str:
    import json
    try:
        return json.loads(os.environ.get('GOOGLE_CREDENTIALS') or '{}').get('client_email', '')
    except ValueError:
        return ''


def _handle_setup_command(word: str, parts: list[str], tenants, reply) -> None:
    """Operator onboarding commands — everything a new client needs, without the console."""
    import re

    import app
    from notimate.projections.sheet_tabs import init_tabs, parse_sheet_id

    if word == 'создать':
        if len(parts) < 5 or not re.fullmatch(TENANT_ID_RE, parts[1]) or parts[2].upper() not in COUNTRY_DEFAULTS or parts[3] not in PACKS:
            reply('Формат: создать <id латиницей, 3–41 символ> <KZ|TH|RU> <monitor|location_reports|accountant> <Название>\nНапример: создать erzhan-cafe KZ monitor Кафе Ержана')
            return
        if any(t['id'] == parts[1] for t in tenants.list_tenants()):
            reply(f'Клиент «{parts[1]}» уже есть.')
            return
        timezone, _currency = COUNTRY_DEFAULTS[parts[2].upper()]
        tenants.upsert_tenant({'id': parts[1], 'name': ' '.join(parts[4:]), 'country': parts[2].upper(), 'timezone': timezone,
                               'vertical_pack': parts[3], 'modules': {}})
        sa = _service_account_email()
        reply(f"✅ Клиент «{' '.join(parts[4:])}» создан (id {parts[1]}, режим {parts[3]}).\nДальше:\n"
              f"1) владелец создаёт Google-таблицу и открывает её как «Редактор» для {sa or 'сервисного аккаунта'};\n"
              f"2) вы: таблица {parts[1]} <ссылка>;\n3) владелец пишет боту — заявка; вы: одобрить <№> {parts[1]} владелец.")
        return

    if len(parts) < 3 or not any(t['id'] == parts[1] for t in tenants.list_tenants()):
        reply('Клиент не найден — список: «клиенты».')
        return
    tenant_id = parts[1]
    if word == 'таблица':
        sheet_id = parse_sheet_id(' '.join(parts[2:]))
        if not sheet_id:
            reply('Не вижу ссылку на Google-таблицу. Формат: таблица <id> <ссылка>')
            return
        full = tenants.get_tenant(tenant_id) or {}
        from notimate.accountant_defaults import currency_for
        currency = (full.get('modules') or {}).get('currency') or currency_for(full.get('country'))
        try:
            created = init_tabs(app.gc, sheet_id, currency)
        except Exception as exc:
            reply(f'Нет доступа к таблице ({type(exc).__name__}). Откройте её как «Редактор» для {_service_account_email() or "сервисного аккаунта"} и повторите.')
            return
        tenants.patch_tenant(tenant_id, sheet_id=sheet_id)
        reply(f'✅ Таблица привязана к «{tenant_id}». Создано вкладок: {len(created)}.')
    elif word == 'группа':
        tenants.patch_tenant(tenant_id, modules_patch={'group': parts[2]})
        reply(f'✅ «{tenant_id}» входит в группу «{parts[2]}». Владельцы нескольких бизнесов группы увидят общую панель и сводку.')
    else:  # режим
        primary = parts[2]
        extras = [p.lstrip('+') for p in parts[3:] if p.lstrip('+') in PACKS]
        if primary not in PACKS:
            reply('Режимы: monitor, location_reports, accountant.')
            return
        tenants.patch_tenant(tenant_id, vertical_pack=primary, modules_patch={'extra_packs': [e for e in extras if e != primary]})
        reply(f"✅ Режим «{tenant_id}»: {primary}" + (f" + {', '.join(extras)}" if extras else ''))


def alert_operator(job, exc: Exception) -> None:
    """A message failed for good (all retries used). Tell the operator, content-free: only the
    channel, event id and error type — never the customer's text."""
    import app
    from logging_utils import get_logger

    operators = sorted(operator_ids())
    if not operators or app.tenant_store is None:
        return
    rows = app.tenant_store.list_channels('whatsapp')
    row = next((r for r in rows), None)
    secret = app.WHATSAPP_SECRETS.get(row['channel']['secret_ref']) if row else None
    if not (row and secret and secret.get('access_token')):
        return
    text = f"⚠️ Сообщение не обработано после всех попыток: {job.webhook_event_id} ({type(exc).__name__}). Клиент мог не получить ответ."
    for operator in operators:
        try:
            app.whatsapp_send_proactive(secret['access_token'], row['channel']['external_id'], operator, text)
        except Exception as send_exc:
            get_logger().warning('operator_alert_failed', extra={'error_type': type(send_exc).__name__})
