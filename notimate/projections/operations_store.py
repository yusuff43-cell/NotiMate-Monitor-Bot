"""PostgreSQL as the emerging source of truth for business events (Этап 4, docs/21).

Dual-write only for now: every Sheets projection write (notimate/projections/sheets.py)
gets a parallel, best-effort write here via the ``record_*_safely`` wrappers, which never
raise — a Postgres hiccup must not stop the owner from getting their Sheets row and
notification, exactly like ``refresh_overview_safely`` never lets a dashboard refresh
failure block a confirmed operation.

Reports still read from Sheets. Switching them to read Postgres instead is a deliberate
follow-up once a week of dual-written data exists to compare against Sheets — docs/21's own
acceptance test for this stage ("вечерняя сводка JSC из PostgreSQL совпадает с той, что
строилась из Sheets, на данных за неделю") needs real accumulated data, not something a
single session can verify on day one.

Every row's ``event_key`` reuses the exact ``{event_id}:{effect}:{index}`` scheme Sheets
already uses for its ``NotiMate Event ID`` column (notimate/projections/sheets.py), so a
retried webhook event is idempotent here the same way it already is in Sheets — just
enforced by a UNIQUE constraint instead of a column scan.
"""

from __future__ import annotations

import json
from typing import Any


def _driver():
    import psycopg
    return psycopg


SCHEMA_SQL = """
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
    status TEXT NOT NULL DEFAULT 'confirmed' CHECK (status IN ('draft', 'confirmed', 'rejected')),
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
"""


class PostgresOperationsStore:
    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError('DATABASE_URL is required')
        self.database_url = database_url

    def initialize(self) -> None:
        with _driver().connect(self.database_url) as conn:
            conn.execute(SCHEMA_SQL)

    def ping(self) -> bool:
        try:
            with _driver().connect(self.database_url, connect_timeout=3) as conn:
                conn.execute('SELECT 1')
            return True
        except Exception:
            return False

    def record_operation(
        self, tenant_id: str, event_key: str, operation_type: str, occurred_on: str,
        amount: float | None, currency: str, counterparty: str, description: str,
        details: dict[str, Any] | None = None,
    ) -> bool:
        """Insert one operation row; returns True if a new row was written, False if it
        already existed (retried event) — mirrors append_rows_once's return convention."""
        with _driver().connect(self.database_url) as conn:
            cursor = conn.execute(
                """
                INSERT INTO operations (
                    tenant_id, event_key, operation_type, occurred_on, amount, currency,
                    counterparty, description, details
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (tenant_id, event_key) DO NOTHING
                """,
                (
                    tenant_id, event_key, operation_type, occurred_on, amount, currency,
                    counterparty, description, json.dumps(details or {}, ensure_ascii=False),
                ),
            )
            return cursor.rowcount == 1

    def record_stock_signal(
        self, tenant_id: str, event_key: str, occurred_on: str, category: str,
        product: str, fridge: str, freezer: str, note: str,
    ) -> bool:
        with _driver().connect(self.database_url) as conn:
            cursor = conn.execute(
                """
                INSERT INTO stock_signals (tenant_id, event_key, occurred_on, category, product, fridge, freezer, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, event_key) DO NOTHING
                """,
                (tenant_id, event_key, occurred_on, category, product, fridge, freezer, note),
            )
            return cursor.rowcount == 1

    def record_issue(self, tenant_id: str, event_key: str, occurred_on: str, message: str, advice: str) -> bool:
        with _driver().connect(self.database_url) as conn:
            cursor = conn.execute(
                """
                INSERT INTO issues (tenant_id, event_key, occurred_on, message, advice)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, event_key) DO NOTHING
                """,
                (tenant_id, event_key, occurred_on, message, advice),
            )
            return cursor.rowcount == 1

    def record_reminder(
        self, tenant_id: str, event_key: str, title: str, expiry_date: str | None,
        added_on: str, note: str,
    ) -> bool:
        with _driver().connect(self.database_url) as conn:
            cursor = conn.execute(
                """
                INSERT INTO reminders (tenant_id, event_key, title, expiry_date, added_on, note)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, event_key) DO NOTHING
                """,
                (tenant_id, event_key, title, expiry_date or None, added_on, note),
            )
            return cursor.rowcount == 1


def _log_failure(event: str, exc: Exception) -> None:
    from logging_utils import get_logger
    get_logger().warning(event, extra={'error_type': type(exc).__name__})


def record_operation_safely(tenant_id, event_key, operation_type, occurred_on, amount, currency, counterparty, description, details=None) -> None:
    import app
    if not app.operations_store:
        return
    try:
        app.operations_store.record_operation(tenant_id, event_key, operation_type, occurred_on, amount, currency, counterparty, description, details)
    except Exception as exc:
        _log_failure('operation_record_failed', exc)


def record_stock_signal_safely(tenant_id, event_key, occurred_on, category, product, fridge, freezer, note) -> None:
    import app
    if not app.operations_store:
        return
    try:
        app.operations_store.record_stock_signal(tenant_id, event_key, occurred_on, category, product, fridge, freezer, note)
    except Exception as exc:
        _log_failure('stock_signal_record_failed', exc)


def record_issue_safely(tenant_id, event_key, occurred_on, message, advice) -> None:
    import app
    if not app.operations_store:
        return
    try:
        app.operations_store.record_issue(tenant_id, event_key, occurred_on, message, advice)
    except Exception as exc:
        _log_failure('issue_record_failed', exc)


def record_reminder_safely(tenant_id, event_key, title, expiry_date, added_on, note) -> None:
    import app
    if not app.operations_store:
        return
    try:
        app.operations_store.record_reminder(tenant_id, event_key, title, expiry_date, added_on, note)
    except Exception as exc:
        _log_failure('reminder_record_failed', exc)
