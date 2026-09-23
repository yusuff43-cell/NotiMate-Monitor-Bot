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
