-- Applied automatically and idempotently by notimate.inbound_store.PostgresInboundStore.initialize()
-- at worker startup. Reference copy only, like migrations/001 and 002; nothing executes it directly.
--
-- Deliberately separate from line_events (migrations/001): this table serves only new,
-- non-LINE channels (WhatsApp first) so adding it can never affect the live LINE pipeline.

CREATE TABLE IF NOT EXISTS inbound_events (
    channel TEXT NOT NULL,
    external_event_id TEXT NOT NULL,
    routing_key TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'retry', 'completed', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    locked_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (channel, external_event_id)
);
CREATE INDEX IF NOT EXISTS inbound_events_claim_idx
    ON inbound_events (status, next_attempt_at, created_at);
