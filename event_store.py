from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

def _driver():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg, dict_row


@dataclass(frozen=True)
class EventJob:
    webhook_event_id: str
    destination: str
    payload: dict[str, Any]
    attempts: int


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS line_events (
    webhook_event_id TEXT PRIMARY KEY,
    destination TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'retry', 'completed', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    locked_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS line_events_claim_idx
    ON line_events (status, next_attempt_at, created_at);

CREATE TABLE IF NOT EXISTS openai_usage_daily (
    usage_date DATE NOT NULL,
    model TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    input_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    reasoning_tokens BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (usage_date, model)
);
"""


def retry_delay_seconds(attempts: int) -> int:
    """Return a bounded exponential delay after a failed attempt."""
    return min(300, 5 * (2 ** max(0, attempts - 1)))


class PostgresEventStore:
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

    def register_events(self, destination: str, events: list[dict[str, Any]]) -> tuple[int, int]:
        accepted = 0
        duplicates = 0
        with _driver()[0].connect(self.database_url) as conn:
            for event in events:
                event_id = str(event.get('webhookEventId') or '').strip()
                if not event_id:
                    raise ValueError('LINE event has no webhookEventId')
                cursor = conn.execute(
                    """
                    INSERT INTO line_events (webhook_event_id, destination, payload)
                    VALUES (%s, %s, %s::jsonb)
                    ON CONFLICT (webhook_event_id) DO NOTHING
                    """,
                    (event_id, destination, json.dumps(event, ensure_ascii=False)),
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
                    SELECT webhook_event_id
                    FROM line_events
                    WHERE (
                        status IN ('pending', 'retry') AND next_attempt_at <= NOW()
                    ) OR (
                        status = 'processing' AND locked_at < NOW() - INTERVAL '10 minutes'
                    )
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE line_events AS event
                SET status = 'processing',
                    attempts = attempts + 1,
                    locked_at = NOW(),
                    updated_at = NOW(),
                    last_error = NULL
                FROM candidate
                WHERE event.webhook_event_id = candidate.webhook_event_id
                RETURNING event.webhook_event_id, event.destination,
                          event.payload, event.attempts
                """
            ).fetchone()
        if not row:
            return None
        return EventJob(
            webhook_event_id=row['webhook_event_id'],
            destination=row['destination'],
            payload=row['payload'],
            attempts=row['attempts'],
        )

    def mark_completed(self, event_id: str) -> None:
        with _driver()[0].connect(self.database_url) as conn:
            conn.execute(
                """
                UPDATE line_events
                SET status = 'completed', completed_at = NOW(), locked_at = NULL,
                    updated_at = NOW(), last_error = NULL
                WHERE webhook_event_id = %s
                """,
                (event_id,),
            )

    def mark_retry(self, event_id: str, error: str, attempts: int) -> None:
        delay = retry_delay_seconds(attempts)
        with _driver()[0].connect(self.database_url) as conn:
            conn.execute(
                """
                UPDATE line_events
                SET status = 'retry', locked_at = NULL, updated_at = NOW(),
                    next_attempt_at = NOW() + (%s * INTERVAL '1 second'),
                    last_error = %s
                WHERE webhook_event_id = %s
                """,
                (delay, error[:2000], event_id),
            )

    def mark_failed(self, event_id: str, error: str) -> None:
        with _driver()[0].connect(self.database_url) as conn:
            conn.execute(
                """
                UPDATE line_events
                SET status = 'failed', locked_at = NULL, updated_at = NOW(),
                    last_error = %s
                WHERE webhook_event_id = %s
                """,
                (error[:2000], event_id),
            )

    def record_openai_usage(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        reasoning_tokens: int,
    ) -> None:
        """Store only daily aggregate API usage, never prompts or response content."""
        with _driver()[0].connect(self.database_url) as conn:
            conn.execute(
                """
                INSERT INTO openai_usage_daily (
                    usage_date, model, requests, input_tokens, output_tokens, reasoning_tokens
                )
                VALUES (
                    (NOW() AT TIME ZONE 'Asia/Bangkok')::date, %s, 1, %s, %s, %s
                )
                ON CONFLICT (usage_date, model) DO UPDATE
                SET requests = openai_usage_daily.requests + 1,
                    input_tokens = openai_usage_daily.input_tokens + EXCLUDED.input_tokens,
                    output_tokens = openai_usage_daily.output_tokens + EXCLUDED.output_tokens,
                    reasoning_tokens = openai_usage_daily.reasoning_tokens + EXCLUDED.reasoning_tokens,
                    updated_at = NOW()
                """,
                (model, max(0, input_tokens), max(0, output_tokens), max(0, reasoning_tokens)),
            )

    def purge_expired_event_data(self, raw_payload_days: int, event_ledger_days: int) -> tuple[int, int]:
        """Minimise retained webhook data while preserving short-lived idempotency metadata.

        Terminal events first lose their source payload and diagnostic text.  Their
        content-free event IDs are retained longer solely to prevent duplicate LINE
        deliveries, then removed as well.
        """
        if raw_payload_days < 1 or event_ledger_days <= raw_payload_days:
            raise ValueError('event retention must be greater than raw payload retention')
        with _driver()[0].connect(self.database_url) as conn:
            wiped = conn.execute(
                """
                UPDATE line_events
                SET payload = '{}'::jsonb, last_error = NULL, updated_at = NOW()
                WHERE status IN ('completed', 'failed')
                  AND COALESCE(completed_at, updated_at) < NOW() - (%s * INTERVAL '1 day')
                  AND (payload <> '{}'::jsonb OR last_error IS NOT NULL)
                """,
                (raw_payload_days,),
            ).rowcount
            deleted = conn.execute(
                """
                DELETE FROM line_events
                WHERE status IN ('completed', 'failed')
                  AND COALESCE(completed_at, updated_at) < NOW() - (%s * INTERVAL '1 day')
                """,
                (event_ledger_days,),
            ).rowcount
        return wiped, deleted
