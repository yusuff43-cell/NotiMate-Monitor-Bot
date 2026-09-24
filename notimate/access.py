"""Access requests for WhatsApp senders (first-stage onboarding, run by the developer).

A person finds the bot's number and writes it the business name and their role («Кафе Ромашка,
отчётность»). If the number is not yet known to the tenant, the bot stores an **access request**,
tells the sender to wait, and pings the operator (``OPERATOR_WHATSAPP_IDS``). Nothing from that
sender is processed — no AI call, no sheet write — until the operator approves with
``deploy/access_requests.py``; approval adds the number to the right list (staff / owner /
accountant / a location) and tells the sender.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS access_requests (
    id BIGSERIAL PRIMARY KEY,
    channel TEXT NOT NULL,
    routing_key TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    message TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    tenant_id TEXT,
    role TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS access_requests_pending_idx
    ON access_requests (channel, routing_key, sender_id) WHERE status = 'pending';
"""

ROLES = ('staff', 'owner', 'accountant')

WAIT_TEXT = ('Здравствуйте! Этот номер ещё не подключён к бизнесу. Заявка отправлена — '
             'ожидайте подтверждения. Если вы ещё не писали, отправьте одним сообщением название бизнеса и вашу роль (например: «Кафе Ромашка, отчётность»).')


def _driver():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg, dict_row


class PostgresAccessStore:
    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError('DATABASE_URL is required')
        self.database_url = database_url

    def initialize(self) -> None:
        with _driver()[0].connect(self.database_url) as conn:
            conn.execute(SCHEMA_SQL)

    def submit(self, channel: str, routing_key: str, sender_id: str, message: str) -> tuple[int, bool]:
        """Create or refresh the sender's pending request; returns ``(id, is_new)``."""
        text = (message or '').strip()[:300]
        with _driver()[0].connect(self.database_url) as conn:
            row = conn.execute(
                """
                INSERT INTO access_requests (channel, routing_key, sender_id, message)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (channel, routing_key, sender_id) WHERE status = 'pending' DO NOTHING
                RETURNING id
                """,
                (channel, routing_key, sender_id, text),
            ).fetchone()
            if row:
                return row[0], True
            existing = conn.execute(
                "SELECT id, message FROM access_requests WHERE channel = %s AND routing_key = %s AND sender_id = %s AND status = 'pending'",
                (channel, routing_key, sender_id),
            ).fetchone()
            if text and text != (existing[1] or '') and not text.startswith('['):
                conn.execute('UPDATE access_requests SET message = %s, updated_at = NOW() WHERE id = %s', (text, existing[0]))
        return existing[0], False

    def get(self, request_id: int) -> dict[str, Any] | None:
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            row = conn.execute('SELECT * FROM access_requests WHERE id = %s', (request_id,)).fetchone()
        return dict(row) if row else None

    def list_pending(self) -> list[dict[str, Any]]:
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            rows = conn.execute("SELECT * FROM access_requests WHERE status = 'pending' ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def decide(self, request_id: int, status: str, tenant_id: str | None = None, role: str | None = None) -> None:
        with _driver()[0].connect(self.database_url) as conn:
            conn.execute(
                'UPDATE access_requests SET status = %s, tenant_id = %s, role = %s, decided_at = NOW(), updated_at = NOW() WHERE id = %s',
                (status, tenant_id, role, request_id),
            )


def sender_is_known(row: Mapping[str, Any], sender_id: str, staff_lookup=None) -> bool:
    """Is this number already connected to the tenant (owner, allowed staff, accountant, or a
    registered location employee for «Отчёты точек»)?"""
    channel = row['channel']
    known = {str(o) for o in (channel.get('owner_ids') or [])} | {str(a) for a in (channel.get('allowed_chats') or [])}
    modules = row['tenant'].get('modules') if isinstance(row['tenant'].get('modules'), Mapping) else {}
    accountant = modules.get('accountant') if isinstance(modules.get('accountant'), Mapping) else {}
    known |= {str(a) for a in accountant.get('accountant_ids') or []}
    if sender_id in known:
        return True
    from notimate.tenants import tenant_packs
    if 'location_reports' in tenant_packs(row['tenant']) and staff_lookup is not None:
        try:
            return bool(staff_lookup(row['tenant']['id'], sender_id))
        except Exception:
            return False
    return False


def request_access(row: Mapping[str, Any], config: Mapping[str, Any], inbound) -> None:
    """Record the request, answer the sender, ping the operator (first time only)."""
    import app
    from logging_utils import get_logger

    logger = get_logger()
    store = getattr(app, 'access_store', None)
    token, pnid = config['access_token'], config['phone_number_id']
    text = inbound.text.strip() or '[фото/файл без текста]'
    is_new = True
    request_id = None
    if store is not None:
        try:
            request_id, is_new = store.submit('whatsapp', row['channel']['external_id'], inbound.sender_id, text)
        except Exception as exc:
            logger.warning('access_request_store_failed', extra={'error_type': type(exc).__name__})
    app.whatsapp_send_text(token, pnid, inbound.sender_id, WAIT_TEXT if is_new else 'Заявка ждёт подтверждения — ответим, как только доступ откроют.')
    if not is_new:
        return
    for operator in [o.strip() for o in os.environ.get('OPERATOR_WHATSAPP_IDS', '').split(',') if o.strip()]:
        try:
            app.whatsapp_send_proactive(
                token, pnid, operator,
                f"🆕 Заявка на доступ №{request_id}: +{inbound.sender_id} → {row['tenant'].get('name') or row['tenant']['id']}\n«{text[:200]}»\n"
                f"Одобрить: python deploy/access_requests.py approve {request_id} --role staff",
            )
        except Exception as exc:
            logger.warning('access_operator_notify_failed', extra={'error_type': type(exc).__name__})


def apply_approval(tenant_store, reports_store, request: Mapping[str, Any], role: str, *, location_id: str | None = None, name: str = '', tenant_id: str | None = None) -> dict[str, Any]:
    """Add the requester to the right list for the tenant that owns the request's number.
    Returns ``{'tenant_id', 'tenant_name', 'role'}``; raises ``ValueError`` on bad input."""
    if role not in ROLES:
        raise ValueError(f'role must be one of {", ".join(ROLES)}')
    shared = tenant_store.shared_number(request['routing_key']) if request['channel'] == 'whatsapp' else None
    if shared:
        return _approve_on_shared_number(tenant_store, reports_store, request, role, tenant_id, location_id, name)
    row = tenant_store.find_channel(request['channel'], request['routing_key'])
    if not row:
        raise ValueError('The number this request came to is not connected to any active tenant')
    tenant, sender = row['tenant'], request['sender_id']
    if role == 'owner':
        tenant_store.add_channel_member(request['channel'], request['routing_key'], 'owner_ids', sender)
    elif role == 'accountant':
        full = tenant_store.get_tenant(tenant['id'])
        modules = dict(full.get('modules') or {})
        accountant = dict(modules.get('accountant') or {})
        ids = [str(i) for i in accountant.get('accountant_ids') or []]
        if sender not in ids:
            ids.append(sender)
        accountant['accountant_ids'] = ids
        accountant.setdefault('enabled', True)
        modules['accountant'] = accountant
        tenant_store.upsert_tenant({**{k: full[k] for k in ('id', 'name', 'country', 'timezone', 'owner_language', 'business_type', 'vertical_pack', 'sheet_id', 'custom_context', 'status')}, 'modules': modules})
        tenant_store.add_channel_member(request['channel'], request['routing_key'], 'allowed_chats', sender)
    else:
        if tenant.get('vertical_pack') == 'location_reports':
            if not location_id:
                raise ValueError('«Отчёты точек»: укажите --location (id точки сотрудника)')
            reports_store.upsert_staff(tenant['id'], sender, location_id, name, 'staff')
        tenant_store.add_channel_member(request['channel'], request['routing_key'], 'allowed_chats', sender)
    return {'tenant_id': tenant['id'], 'tenant_name': tenant.get('name') or tenant['id'], 'role': role}


def _approve_on_shared_number(tenant_store, reports_store, request, role, tenant_id, location_id, name) -> dict[str, Any]:
    """On a shared number the operator names the business explicitly; the requester is then
    added to that business only (``tenant_members``)."""
    if not tenant_id:
        raise ValueError('На общем номере нужно указать id клиента (команда «клиенты» покажет список)')
    tenant = tenant_store.get_tenant(tenant_id)
    if not tenant:
        raise ValueError(f'Клиент «{tenant_id}» не найден или неактивен')
    if role == 'staff' and tenant.get('vertical_pack') == 'location_reports':
        if not location_id:
            raise ValueError('«Отчёты точек»: укажите точку — «точка:<id>»')
        reports_store.upsert_staff(tenant_id, request['sender_id'], location_id, name, 'staff')
    tenant_store.add_member(tenant_id, request['routing_key'], request['sender_id'], role, name, location_id)
    return {'tenant_id': tenant_id, 'tenant_name': tenant.get('name') or tenant_id, 'role': role}


def request_access_shared(phone_number_id: str, config: Mapping[str, Any], sender_id: str, text: str) -> None:
    """Unknown number on a shared bot number: store a request (no business is chosen yet),
    answer the sender, and tell the operator how to approve (first message only)."""
    import app
    from logging_utils import get_logger

    logger = get_logger()
    store = getattr(app, 'access_store', None)
    token = config['access_token']
    is_new, request_id = True, None
    if store is not None:
        try:
            request_id, is_new = store.submit('whatsapp', phone_number_id, sender_id, text)
        except Exception as exc:
            logger.warning('access_request_store_failed', extra={'error_type': type(exc).__name__})
    app.whatsapp_send_text(token, phone_number_id, sender_id, WAIT_TEXT if is_new else 'Заявка ждёт подтверждения — ответим, как только доступ откроют.')
    if not is_new:
        return
    listing = ''
    try:
        listing = '\n'.join(f"• {t['id']} — {t['name']}" for t in app.tenant_store.list_tenants()[:12])
    except Exception:
        pass
    for operator in [o.strip() for o in os.environ.get('OPERATOR_WHATSAPP_IDS', '').split(',') if o.strip()]:
        try:
            app.whatsapp_send_proactive(
                token, phone_number_id, operator,
                f"🆕 Заявка #{request_id}: +{sender_id}\n«{text[:200]}»\nОдобрить: одобрить {request_id} <id клиента> [сотрудник|владелец|бухгалтер]\n\nКлиенты:\n{listing}",
            )
        except Exception as exc:
            logger.warning('access_operator_notify_failed', extra={'error_type': type(exc).__name__})
