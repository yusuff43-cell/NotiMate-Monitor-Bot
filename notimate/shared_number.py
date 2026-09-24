"""One WhatsApp number for many businesses: routing by SENDER.

Isolation rules (why a report can not reach the wrong business):

1. The business is chosen only from the ``tenant_members`` table by the sender's WhatsApp id
   (the phone number WhatsApp itself vouches for in a Meta-signed webhook). Nothing the sender
   types — a business name, a tenant id — ever selects a tenant. A new number can only create an
   access *request*; the operator decides which business it joins.
2. A person who belongs to several businesses must pick one (buttons); the choice expires after
   12 hours. While no valid choice exists their messages are NOT processed — the bot asks again —
   so nothing is silently filed under the wrong business.
3. Every draft/document button is checked against ``tenant_id`` and ``sender_id`` of the record
   (see the packs), and every stored row carries the tenant id and the sender's number.
4. Replies name the business («✅ Принято (Кафе Ромашка)») so a mistake is visible immediately.
"""

from __future__ import annotations

from typing import Any

from logging_utils import get_logger

logger = get_logger()

CONTEXT_HOURS = 12
SWITCH_COMMANDS = ('бизнес', 'сменить бизнес', 'switch', 'business')


def message_text(message: dict[str, Any]) -> str:
    if message.get('type') == 'text':
        return str(message.get('text', {}).get('body') or '')
    if message.get('type') == 'interactive':
        interactive = message.get('interactive', {})
        return str(interactive.get('button_reply', {}).get('id') or interactive.get('list_reply', {}).get('id') or '')
    caption = (message.get('image') or message.get('document') or {}).get('caption')
    return str(caption or '')


def _prompt_choice(app, config, sender_id: str, memberships: list[dict[str, Any]]) -> None:
    token, pnid = config['access_token'], config['phone_number_id']
    if len(memberships) <= 3:
        app.whatsapp_send_interactive_buttons(
            token, pnid, sender_id, 'Вы подключены к нескольким бизнесам. Выберите, с каким работаем сейчас (выбор действует 12 часов):',
            [(f"ctx:{m['tenant_id']}", str(m['tenant_name'])[:20]) for m in memberships],
        )
    else:
        lines = '\n'.join(f"{i}. {m['tenant_name']}" for i, m in enumerate(memberships, 1))
        app.whatsapp_send_text(token, pnid, sender_id, f'Вы подключены к нескольким бизнесам:\n{lines}\nОтветьте: «бизнес 1», «бизнес 2» …')


def resolve_shared(phone_number_id: str, message: dict[str, Any]):
    """Resolve one inbound message on a shared number.

    Returns ``(row, None)`` when the sender's business is known, ``(None, 'handled')`` when the
    bot already answered (choice prompt, access request, operator command …) and
    ``(None, 'unknown')`` when the number is not a shared number at all.
    """
    import app
    from notimate.access import request_access_shared
    from notimate.operator import handle_operator_command, is_operator

    store = app.tenant_store
    shared = store.shared_number(phone_number_id) if store is not None else None
    if not shared:
        return None, 'unknown'
    secret = app.WHATSAPP_SECRETS.get(shared['secret_ref']) or {}
    token = secret.get('access_token')
    if not token:
        raise RuntimeError('Shared WhatsApp number has no access token configured')
    config = {'access_token': token, 'phone_number_id': phone_number_id}
    sender_id = str(message.get('from') or '')
    text = message_text(message).strip()
    lowered = text.lower().lstrip('/')

    if is_operator(sender_id) and handle_operator_command(phone_number_id, config, sender_id, text):
        return None, 'handled'

    memberships = store.memberships(phone_number_id, sender_id)
    if not memberships:
        request_access_shared(phone_number_id, config, sender_id, text or '[фото/файл без текста]')
        return None, 'handled'

    by_id = {m['tenant_id']: m for m in memberships}
    if text.startswith('ctx:'):
        chosen = by_id.get(text[4:])
        if chosen is None:
            app.whatsapp_send_text(token, phone_number_id, sender_id, 'Этот бизнес вам недоступен.')
        else:
            store.set_context(phone_number_id, sender_id, chosen['tenant_id'])
            app.whatsapp_send_text(token, phone_number_id, sender_id, f"✅ Работаем с бизнесом «{chosen['tenant_name']}». Напишите «бизнес», чтобы сменить.")
        return None, 'handled'
    if lowered.split(' ', 1)[0] in SWITCH_COMMANDS and len(memberships) > 1:
        parts = lowered.split()
        if len(parts) == 2 and parts[1].isdigit() and 1 <= int(parts[1]) <= len(memberships):
            chosen = memberships[int(parts[1]) - 1]
            store.set_context(phone_number_id, sender_id, chosen['tenant_id'])
            app.whatsapp_send_text(token, phone_number_id, sender_id, f"✅ Работаем с бизнесом «{chosen['tenant_name']}».")
        else:
            _prompt_choice(app, config, sender_id, memberships)
        return None, 'handled'

    if len(memberships) == 1:
        tenant_id = memberships[0]['tenant_id']
    else:
        tenant_id = store.get_context(phone_number_id, sender_id, CONTEXT_HOURS)
        if tenant_id not in by_id:
            _prompt_choice(app, config, sender_id, memberships)
            app.whatsapp_send_text(token, phone_number_id, sender_id, 'Сообщение не обработано — выберите бизнес и отправьте его ещё раз.')
            return None, 'handled'
        store.set_context(phone_number_id, sender_id, tenant_id)  # active use keeps the choice alive
    row = store.shared_row(tenant_id, phone_number_id)
    if row is None:
        app.whatsapp_send_text(token, phone_number_id, sender_id, 'Этот бизнес сейчас недоступен. Обратитесь к владельцу.')
        return None, 'handled'
    return row, None
