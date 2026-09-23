-- Applied automatically and idempotently by
-- notimate.packs.location_reports.PostgresLocationReportsStore.initialize() at worker
-- startup. Reference copy only, like migrations/001-004; nothing executes it directly.
--
-- Этап 6 (docs/21): «Отчёты точек» — locations/staff reference data plus the draft →
-- confirm/cancel flow ported from Клиенты/Ержан — СтройКонтроль/src/stroycontrol.
-- Only tenants with tenants.vertical_pack = 'location_reports' use these tables.

CREATE TABLE IF NOT EXISTS locations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS locations_tenant_idx ON locations (tenant_id);

CREATE TABLE IF NOT EXISTS staff (
    tenant_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    location_id TEXT NOT NULL REFERENCES locations(id),
    name TEXT,
    role TEXT NOT NULL DEFAULT 'staff',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, sender_id)
);

CREATE TABLE IF NOT EXISTS location_report_drafts (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    location_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    occurred_on DATE NOT NULL,
    revenue NUMERIC,
    cash NUMERIC,
    non_cash NUMERIC,
    external_payouts NUMERIC,
    cash_balance NUMERIC,
    comment TEXT,
    source_text TEXT,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'confirmed', 'cancelled')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS location_reports (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    location_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    occurred_on DATE NOT NULL,
    revenue NUMERIC,
    cash NUMERIC,
    non_cash NUMERIC,
    external_payouts NUMERIC,
    cash_balance NUMERIC,
    comment TEXT,
    confirmed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS location_reports_tenant_date_idx ON location_reports (tenant_id, occurred_on);
