"""Durable queue for non-LINE inbound events (Этап 3 of docs/21): `inbound_events`.

Mirrors event_store.py's proven job-queue pattern (claim/complete/retry/fail, same retry
backoff, same terminal-failure and stuck-`processing` recovery rules) against a new,
additive table instead of touching `line_events`. `claim_next` returns the exact same
`EventJob` shape event_store.py defines, so `worker.run_once` works against either store
completely unchanged — the field named `destination` here holds the channel's own routing
key (a WhatsApp `phone_number_id`), not a LINE destination; the name is just reused, not
LINE-specific.
"""

from __future__ import annotations

import json
from typing import Any

from event_store import EventJob, retry_delay_seconds  # noqa: F401  (re-exported for callers)


def _driver():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg, dict_row


SCHEMA_SQL = """
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
"""


class PostgresInboundStore:
    def __init__(self, database_url: str, channel: str):
        if not database_url:
            raise ValueError('DATABASE_URL is required')
        if not channel:
            raise ValueError('channel is required')
        self.database_url = database_url
        self.channel = channel

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

    def register_events(self, routing_key: str, events: list[dict[str, Any]]) -> tuple[int, int]:
        """Insert events keyed by (self.channel, event['id']); returns (accepted, duplicates)."""
        accepted = 0
        duplicates = 0
        with _driver()[0].connect(self.database_url) as conn:
            for event in events:
                event_id = str(event.get('id') or '').strip()
                if not event_id:
                    raise ValueError('inbound message has no id')
                cursor = conn.execute(
                    """
                    INSERT INTO inbound_events (channel, external_event_id, routing_key, payload)
                    VALUES (%s, %s, %s, %s::jsonb)
                    ON CONFLICT (channel, external_event_id) DO NOTHING
                    """,
                    (self.channel, event_id, routing_key, json.dumps(event, ensure_ascii=False)),
                )
                if cursor.rowcount == 1:
                    accepted += 1
                else:
                    duplicates += 1
        return accepted, duplicates

    def claim_next(self) -> EventJob | None:
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            row = conn.execute(
                """
                WITH candidate AS (
                    SELECT channel, external_event_id
                    FROM inbound_events
                    WHERE channel = %s AND (
                        (status IN ('pending', 'retry') AND next_attempt_at <= NOW())
                        OR (status = 'processing' AND locked_at < NOW() - INTERVAL '10 minutes')
                    )
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE inbound_events AS event
                SET status = 'processing', attempts = attempts + 1, locked_at = NOW(),
                    updated_at = NOW(), last_error = NULL
                FROM candidate
                WHERE event.channel = candidate.channel
                  AND event.external_event_id = candidate.external_event_id
                RETURNING event.external_event_id, event.routing_key, event.payload, event.attempts
                """,
                (self.channel,),
            ).fetchone()
        if not row:
            return None
        return EventJob(
            webhook_event_id=row['external_event_id'],
            destination=row['routing_key'],
            payload=row['payload'],
            attempts=row['attempts'],
        )

    def mark_completed(self, event_id: str) -> None:
        with _driver()[0].connect(self.database_url) as conn:
            conn.execute(
                """
                UPDATE inbound_events
                SET status = 'completed', completed_at = NOW(), locked_at = NULL,
                    updated_at = NOW(), last_error = NULL
                WHERE channel = %s AND external_event_id = %s
                """,
                (self.channel, event_id),
            )

    def mark_retry(self, event_id: str, error: str, attempts: int) -> None:
        delay = retry_delay_seconds(attempts)
        with _driver()[0].connect(self.database_url) as conn:
            conn.execute(
                """
                UPDATE inbound_events
                SET status = 'retry', locked_at = NULL, updated_at = NOW(),
                    next_attempt_at = NOW() + (%s * INTERVAL '1 second'),
                    last_error = %s
                WHERE channel = %s AND external_event_id = %s
                """,
                (delay, error[:2000], self.channel, event_id),
            )

    def mark_failed(self, event_id: str, error: str) -> None:
        with _driver()[0].connect(self.database_url) as conn:
            conn.execute(
                """
                UPDATE inbound_events
                SET status = 'failed', locked_at = NULL, updated_at = NOW(), last_error = %s
                WHERE channel = %s AND external_event_id = %s
                """,
                (error[:2000], self.channel, event_id),
            )

    def purge_expired_event_data(self, raw_payload_days: int, event_ledger_days: int) -> tuple[int, int]:
        """Same retention policy as event_store.py's line_events (14/90 days by default)."""
        if raw_payload_days < 1 or event_ledger_days <= raw_payload_days:
            raise ValueError('event retention must be greater than raw payload retention')
        with _driver()[0].connect(self.database_url) as conn:
            wiped = conn.execute(
                """
                UPDATE inbound_events
                SET payload = '{}'::jsonb, last_error = NULL, updated_at = NOW()
                WHERE channel = %s AND status IN ('completed', 'failed')
                  AND COALESCE(completed_at, updated_at) < NOW() - (%s * INTERVAL '1 day')
                  AND (payload <> '{}'::jsonb OR last_error IS NOT NULL)
                """,
                (self.channel, raw_payload_days),
            ).rowcount
            deleted = conn.execute(
                """
                DELETE FROM inbound_events
                WHERE channel = %s AND status IN ('completed', 'failed')
                  AND COALESCE(completed_at, updated_at) < NOW() - (%s * INTERVAL '1 day')
                """,
                (self.channel, event_ledger_days),
            ).rowcount
        return wiped, deleted
