-- Applied automatically and idempotently by notimate.projections.operations_store.
-- PostgresOperationsStore.initialize() at process startup. Reference copy only, like
-- migrations/001-003; nothing executes it directly.
--
-- Этап 4 (docs/21): PostgreSQL as the emerging source of truth for business events.
-- Dual-write only for now — Sheets stays the read path for reports until a week of
-- dual-written data can be compared against it (see docs/21's acceptance test and
-- docs/05 Development Log for why this is deliberately staged).

CREATE TABLE IF NOT EXISTS operations (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    event_key TEXT NOT NULL,
    operation_type TEXT NOT NULL CHECK (operation_type IN ('purchase', 'expense', 'revenue', 'salary')),
    occurred_on DATE NOT NULL,
    amount NUMERIC,
    currency TEXT NOT NULL DEFAULT 'THB',
    counterparty TEXT,
    description TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'confirmed'
        CHECK (status IN ('draft', 'confirmed', 'rejected')),
    version INTEGER NOT NULL DEFAULT 1,
    confirmed_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, event_key)
);
CREATE INDEX IF NOT EXISTS operations_tenant_date_idx ON operations (tenant_id, occurred_on);

CREATE TABLE IF NOT EXISTS stock_signals (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    event_key TEXT NOT NULL,
    occurred_on DATE NOT NULL,
    category TEXT,
    product TEXT NOT NULL,
    fridge TEXT,
    freezer TEXT,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, event_key)
);
CREATE INDEX IF NOT EXISTS stock_signals_tenant_date_idx ON stock_signals (tenant_id, occurred_on);

CREATE TABLE IF NOT EXISTS issues (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    event_key TEXT NOT NULL,
    occurred_on DATE NOT NULL,
    message TEXT NOT NULL,
    advice TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, event_key)
);

CREATE TABLE IF NOT EXISTS reminders (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    event_key TEXT NOT NULL,
    title TEXT NOT NULL,
    expiry_date DATE,
    added_on DATE,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, event_key)
);
