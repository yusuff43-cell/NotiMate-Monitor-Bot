"""PostgreSQL-backed tenant and channel configuration (Этап 2 of docs/21).

``CLIENTS_JSON`` stays the fallback source through Этап 3 (docs/21): once a tenant is
imported (``deploy/import_tenants_from_clients_json.py``), ``notimate.tenants.resolve_client``
prefers this store; an unmigrated destination, or any row this store cannot validate, still
resolves from ``CLIENTS_JSON`` exactly as before this file existed.

Secrets are never stored here. ``tenant_channels.secret_ref`` only names where to find the
channel's credentials; today it is the ``CLIENTS_JSON`` destination key that holds them, so
resolving a channel still means reading the real secret out of the server's own environment.
"""

from __future__ import annotations

import json
from typing import Any


def _driver():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg, dict_row


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    country TEXT NOT NULL,
    timezone TEXT NOT NULL,
    owner_language TEXT NOT NULL DEFAULT 'ru',
    business_type TEXT,
    vertical_pack TEXT,
    modules JSONB NOT NULL DEFAULT '{}'::jsonb,
    sheet_id TEXT,
    custom_context TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'archived')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tenant_channels (
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    channel TEXT NOT NULL CHECK (channel IN ('line', 'whatsapp', 'telegram')),
    external_id TEXT NOT NULL,
    secret_ref TEXT NOT NULL,
    allowed_chats JSONB,
    owner_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (channel, external_id)
);
CREATE INDEX IF NOT EXISTS tenant_channels_tenant_idx ON tenant_channels (tenant_id);

-- Shared WhatsApp numbers: one bot number serves many businesses. A message is routed by its
-- SENDER (tenant_members), never by anything the sender writes, and only for numbers listed here.
CREATE TABLE IF NOT EXISTS whatsapp_shared_numbers (
    phone_number_id TEXT PRIMARY KEY,
    secret_ref TEXT NOT NULL,
    label TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tenant_members (
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    phone_number_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner', 'staff', 'accountant')),
    name TEXT,
    location_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, phone_number_id, sender_id)
);
CREATE INDEX IF NOT EXISTS tenant_members_sender_idx ON tenant_members (phone_number_id, sender_id);

-- Which business a person who belongs to several is currently writing to (expires; see notimate/shared_number.py).
CREATE TABLE IF NOT EXISTS sender_context (
    phone_number_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (phone_number_id, sender_id)
);
"""


class PostgresTenantStore:
    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError('DATABASE_URL is required')
        self.database_url = database_url

    def initialize(self) -> None:
        with _driver()[0].connect(self.database_url) as conn:
            conn.execute(SCHEMA_SQL)

    def ping(self) -> bool:
        try:
            with _driver()[0].connect(self.database_url, connect_timeout=3) as conn:
                conn.execute('SELECT 1')
            return True
        except Exception:
            return False

    def upsert_tenant(self, tenant: dict[str, Any]) -> None:
        with _driver()[0].connect(self.database_url) as conn:
            conn.execute(
                """
                INSERT INTO tenants (
                    id, name, country, timezone, owner_language, business_type,
                    vertical_pack, modules, sheet_id, custom_context, status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    country = EXCLUDED.country,
                    timezone = EXCLUDED.timezone,
                    owner_language = EXCLUDED.owner_language,
                    business_type = EXCLUDED.business_type,
                    vertical_pack = EXCLUDED.vertical_pack,
                    modules = EXCLUDED.modules,
                    sheet_id = EXCLUDED.sheet_id,
                    custom_context = EXCLUDED.custom_context,
                    status = EXCLUDED.status,
                    updated_at = NOW()
                """,
                (
                    tenant['id'],
                    tenant['name'],
                    tenant['country'],
                    tenant['timezone'],
                    tenant.get('owner_language', 'ru'),
                    tenant.get('business_type'),
                    tenant.get('vertical_pack'),
                    json.dumps(tenant.get('modules') or {}, ensure_ascii=False),
                    tenant.get('sheet_id'),
                    tenant.get('custom_context'),
                    tenant.get('status', 'active'),
                ),
            )

    def upsert_channel(self, channel: dict[str, Any]) -> None:
        with _driver()[0].connect(self.database_url) as conn:
            conn.execute(
                """
                INSERT INTO tenant_channels (
                    tenant_id, channel, external_id, secret_ref, allowed_chats, owner_ids
                )
                VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)
                ON CONFLICT (channel, external_id) DO UPDATE SET
                    tenant_id = EXCLUDED.tenant_id,
                    secret_ref = EXCLUDED.secret_ref,
                    allowed_chats = EXCLUDED.allowed_chats,
                    owner_ids = EXCLUDED.owner_ids,
                    updated_at = NOW()
                """,
                (
                    channel['tenant_id'],
                    channel['channel'],
                    channel['external_id'],
                    channel['secret_ref'],
                    json.dumps(channel['allowed_chats'], ensure_ascii=False)
                    if channel.get('allowed_chats') is not None else None,
                    json.dumps(channel.get('owner_ids') or [], ensure_ascii=False),
                ),
            )

    def find_channel(self, channel: str, external_id: str) -> dict[str, Any] | None:
        """Return the merged tenant+channel row for one (channel, external_id), or None.

        The composite primary key on ``tenant_channels`` is exactly (channel, external_id):
        two tenants can never share one row here, so a LINE destination and a WhatsApp
        phone number resolve independently even if both happen to collide as raw strings.
        """
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            row = conn.execute(
                """
                SELECT
                    t.id AS tenant_id, t.name, t.country, t.timezone, t.owner_language,
                    t.business_type, t.vertical_pack, t.modules, t.sheet_id,
                    t.custom_context, t.status,
                    c.channel, c.external_id, c.secret_ref, c.allowed_chats, c.owner_ids
                FROM tenant_channels c
                JOIN tenants t ON t.id = c.tenant_id
                WHERE c.channel = %s AND c.external_id = %s AND t.status = 'active'
                """,
                (channel, external_id),
            ).fetchone()
        if not row:
            return None
        return {
            'tenant': {
                'id': row['tenant_id'],
                'name': row['name'],
                'country': row['country'],
                'timezone': row['timezone'],
                'owner_language': row['owner_language'],
                'business_type': row['business_type'],
                'vertical_pack': row['vertical_pack'],
                'modules': row['modules'],
                'sheet_id': row['sheet_id'],
                'custom_context': row['custom_context'],
                'status': row['status'],
            },
            'channel': {
                'channel': row['channel'],
                'external_id': row['external_id'],
                'secret_ref': row['secret_ref'],
                'allowed_chats': row['allowed_chats'],
                'owner_ids': row['owner_ids'],
            },
        }

    def list_channels(self, channel: str) -> list[dict[str, Any]]:
        """List every active tenant+channel row for one channel — used to enumerate
        WhatsApp tenants for per-tenant scheduled jobs (Этап 6's evening summary)."""
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            rows = conn.execute(
                """
                SELECT c.external_id
                FROM tenant_channels c
                JOIN tenants t ON t.id = c.tenant_id
                WHERE c.channel = %s AND t.status = 'active'
                """,
                (channel,),
            ).fetchall()
        found = [row for row in (self.find_channel(channel, r['external_id']) for r in rows) if row]
        if channel == 'whatsapp':
            found += self.list_shared_rows()
        return found

    def get_tenant(self, tenant_id: str) -> dict[str, Any] | None:
        """Active tenant by id, with ``owner_ids`` merged across all its channels.

        Used to re-authorize every dashboard/API request against current data instead of
        trusting whatever a session token said when it was issued (removing an owner from
        ``tenant_channels`` immediately cuts off their existing sessions).
        """
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            row = conn.execute(
                """
                SELECT id, name, country, timezone, owner_language, business_type,
                       vertical_pack, modules, sheet_id, custom_context, status
                FROM tenants WHERE id = %s AND status = 'active'
                """,
                (tenant_id,),
            ).fetchone()
            if not row:
                return None
            owners = conn.execute('SELECT channel, owner_ids FROM tenant_channels WHERE tenant_id = %s', (tenant_id,)).fetchall()
            members = conn.execute('SELECT sender_id, role FROM tenant_members WHERE tenant_id = %s', (tenant_id,)).fetchall()
        tenant = dict(row)
        merged: list[str] = []
        for record in owners:
            for owner in record['owner_ids'] or []:
                if str(owner) not in merged:
                    merged.append(str(owner))
        for member in members:
            if member['role'] == 'owner' and str(member['sender_id']) not in merged:
                merged.append(str(member['sender_id']))
        tenant['owner_ids'] = merged
        accountants = [m['sender_id'] for m in members if m['role'] == 'accountant']
        if accountants:
            modules = dict(tenant.get('modules') or {})
            accountant = dict(modules.get('accountant') or {})
            accountant['accountant_ids'] = list(dict.fromkeys([str(a) for a in accountant.get('accountant_ids') or []] + accountants))
            modules['accountant'] = accountant
            tenant['modules'] = modules
        tenant['channels'] = sorted({record['channel'] for record in owners} | ({'whatsapp'} if members else set()))
        return tenant

    def add_channel_member(self, channel: str, external_id: str, field: str, member: str) -> None:
        """Append one id to a channel's ``owner_ids`` or ``allowed_chats`` list (idempotent)."""
        if field not in ('owner_ids', 'allowed_chats'):
            raise ValueError('field must be owner_ids or allowed_chats')
        with _driver()[0].connect(self.database_url) as conn:
            conn.execute(
                f"""
                UPDATE tenant_channels
                SET {field} = COALESCE({field}, '[]'::jsonb) || to_jsonb(%s::text), updated_at = NOW()
                WHERE channel = %s AND external_id = %s
                  AND NOT (COALESCE({field}, '[]'::jsonb) @> to_jsonb(%s::text))
                """,
                (member, channel, external_id, member),
            )

    # ── shared WhatsApp numbers ────────────────────────────────────────────────────────
    def mark_shared(self, phone_number_id: str, secret_ref: str, label: str = '') -> None:
        with _driver()[0].connect(self.database_url) as conn:
            conn.execute(
                """
                INSERT INTO whatsapp_shared_numbers (phone_number_id, secret_ref, label) VALUES (%s, %s, %s)
                ON CONFLICT (phone_number_id) DO UPDATE SET secret_ref = EXCLUDED.secret_ref, label = EXCLUDED.label
                """,
                (phone_number_id, secret_ref, label),
            )

    def shared_number(self, phone_number_id: str) -> dict[str, Any] | None:
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            row = conn.execute('SELECT * FROM whatsapp_shared_numbers WHERE phone_number_id = %s', (phone_number_id,)).fetchone()
        return dict(row) if row else None

    def add_member(self, tenant_id: str, phone_number_id: str, sender_id: str, role: str, name: str = '', location_id: str | None = None) -> None:
        with _driver()[0].connect(self.database_url) as conn:
            conn.execute(
                """
                INSERT INTO tenant_members (tenant_id, phone_number_id, sender_id, role, name, location_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, phone_number_id, sender_id)
                DO UPDATE SET role = EXCLUDED.role, name = EXCLUDED.name, location_id = EXCLUDED.location_id
                """,
                (tenant_id, phone_number_id, sender_id, role, name, location_id),
            )

    def remove_member(self, tenant_id: str, phone_number_id: str, sender_id: str) -> None:
        with _driver()[0].connect(self.database_url) as conn:
            conn.execute('DELETE FROM tenant_members WHERE tenant_id = %s AND phone_number_id = %s AND sender_id = %s', (tenant_id, phone_number_id, sender_id))
            conn.execute('DELETE FROM sender_context WHERE phone_number_id = %s AND sender_id = %s AND tenant_id = %s', (phone_number_id, sender_id, tenant_id))

    def memberships(self, phone_number_id: str, sender_id: str) -> list[dict[str, Any]]:
        """Active businesses this number belongs to (a paused/archived tenant never routes)."""
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            rows = conn.execute(
                """
                SELECT m.tenant_id, m.role, m.name, m.location_id, t.name AS tenant_name
                FROM tenant_members m JOIN tenants t ON t.id = m.tenant_id
                WHERE m.phone_number_id = %s AND m.sender_id = %s AND t.status = 'active'
                ORDER BY t.name
                """,
                (phone_number_id, sender_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_context(self, phone_number_id: str, sender_id: str, max_age_hours: int = 12) -> str | None:
        with _driver()[0].connect(self.database_url) as conn:
            row = conn.execute(
                """
                SELECT tenant_id FROM sender_context
                WHERE phone_number_id = %s AND sender_id = %s AND updated_at > NOW() - make_interval(hours => %s)
                """,
                (phone_number_id, sender_id, max_age_hours),
            ).fetchone()
        return row[0] if row else None

    def set_context(self, phone_number_id: str, sender_id: str, tenant_id: str) -> None:
        with _driver()[0].connect(self.database_url) as conn:
            conn.execute(
                """
                INSERT INTO sender_context (phone_number_id, sender_id, tenant_id) VALUES (%s, %s, %s)
                ON CONFLICT (phone_number_id, sender_id) DO UPDATE SET tenant_id = EXCLUDED.tenant_id, updated_at = NOW()
                """,
                (phone_number_id, sender_id, tenant_id),
            )

    def shared_row(self, tenant_id: str, phone_number_id: str) -> dict[str, Any] | None:
        """A ``find_channel``-shaped row for one business on a shared number, so every pack works
        unchanged: owner/staff/accountant lists come from ``tenant_members`` of THAT tenant only."""
        shared = self.shared_number(phone_number_id)
        if not shared:
            return None
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            t = conn.execute(
                """
                SELECT id, name, country, timezone, owner_language, business_type, vertical_pack, modules, sheet_id, custom_context, status
                FROM tenants WHERE id = %s AND status = 'active'
                """,
                (tenant_id,),
            ).fetchone()
            if not t:
                return None
            members = conn.execute(
                'SELECT sender_id, role FROM tenant_members WHERE tenant_id = %s AND phone_number_id = %s ORDER BY created_at',
                (tenant_id, phone_number_id),
            ).fetchall()
        tenant = dict(t)
        modules = dict(tenant.get('modules') or {})
        accountants = [m['sender_id'] for m in members if m['role'] == 'accountant']
        if accountants:
            accountant = dict(modules.get('accountant') or {})
            accountant['accountant_ids'] = list(dict.fromkeys([str(a) for a in accountant.get('accountant_ids') or []] + accountants))
            modules['accountant'] = accountant
        tenant['modules'] = modules
        return {
            'tenant': {k: tenant[k] for k in ('id', 'name', 'country', 'timezone', 'owner_language', 'business_type', 'vertical_pack', 'modules', 'sheet_id', 'custom_context', 'status')},
            'channel': {
                'channel': 'whatsapp', 'external_id': phone_number_id, 'secret_ref': shared['secret_ref'], 'shared': True,
                'owner_ids': [m['sender_id'] for m in members if m['role'] == 'owner'],
                'allowed_chats': [m['sender_id'] for m in members if m['role'] == 'staff'],
            },
        }

    def list_shared_rows(self) -> list[dict[str, Any]]:
        with _driver()[0].connect(self.database_url) as conn:
            pairs = conn.execute(
                """
                SELECT DISTINCT m.tenant_id, m.phone_number_id FROM tenant_members m
                JOIN whatsapp_shared_numbers n ON n.phone_number_id = m.phone_number_id
                JOIN tenants t ON t.id = m.tenant_id AND t.status = 'active'
                """
            ).fetchall()
        return [row for row in (self.shared_row(t, p) for t, p in pairs) if row]

    def list_tenants(self) -> list[dict[str, Any]]:
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            rows = conn.execute("SELECT id, name, country, vertical_pack FROM tenants WHERE status = 'active' ORDER BY name").fetchall()
        return [dict(row) for row in rows]

    def patch_tenant(self, tenant_id: str, *, name: str | None = None, sheet_id: str | None = None,
                     vertical_pack: str | None = None, modules_patch: dict[str, Any] | None = None) -> bool:
        """Change a few fields of an existing tenant; ``modules_patch`` is merged key by key."""
        with _driver()[0].connect(self.database_url) as conn:
            cursor = conn.execute(
                """
                UPDATE tenants SET
                    name = COALESCE(%s, name), sheet_id = COALESCE(%s, sheet_id),
                    vertical_pack = COALESCE(%s, vertical_pack),
                    modules = modules || %s::jsonb, updated_at = NOW()
                WHERE id = %s
                """,
                (name, sheet_id, vertical_pack, json.dumps(modules_patch or {}, ensure_ascii=False), tenant_id),
            )
            return cursor.rowcount == 1

    def group_tenants(self, group_key: str, subject: str | None = None) -> list[dict[str, Any]]:
        """Active businesses of one group (``modules.group``). With ``subject`` only those the
        person OWNS — a dashboard/summary can never show a business the viewer isn't an owner of."""
        with _driver()[0].connect(self.database_url) as conn:
            ids = [r[0] for r in conn.execute(
                """
                SELECT t.id FROM tenants t
                WHERE t.status = 'active' AND t.modules->>'group' = %s
                  AND (%s::text IS NULL
                       OR EXISTS (SELECT 1 FROM tenant_channels c WHERE c.tenant_id = t.id AND c.owner_ids @> to_jsonb(%s::text))
                       OR EXISTS (SELECT 1 FROM tenant_members m WHERE m.tenant_id = t.id AND m.role = 'owner' AND m.sender_id = %s))
                ORDER BY t.name
                """,
                (group_key, subject, subject, subject),
            ).fetchall()]
        return [t for t in (self.get_tenant(i) for i in ids) if t]

    def list_groups(self) -> list[str]:
        with _driver()[0].connect(self.database_url) as conn:
            return [r[0] for r in conn.execute(
                "SELECT DISTINCT modules->>'group' FROM tenants WHERE status = 'active' AND modules ? 'group' AND modules->>'group' <> ''"
            ).fetchall()]
