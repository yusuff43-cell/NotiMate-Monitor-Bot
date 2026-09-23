-- Applied automatically and idempotently by notimate.tenant_store.PostgresTenantStore.initialize()
-- at process startup, exactly like migrations/001_line_events.sql mirrors event_store.py. This
-- file is a readable reference copy; nothing executes it directly.

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
