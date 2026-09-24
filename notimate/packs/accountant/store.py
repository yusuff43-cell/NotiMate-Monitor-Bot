"""PostgreSQL storage for «Бухгалтер»: documents, monthly numbering, accountant questions.

Numbering ``YYYY-MM-NNN`` is allocated only when a document is *confirmed* (a draft that is
cancelled never burns a number, so the paper-original envelope has no gaps) and inside the
same transaction that flips the status, under a row lock on the per-(tenant, period)
counter — two documents confirmed at the same moment can never share a number, and a
repeated confirm tap raises ``DocumentAlreadyFinalized`` instead of numbering twice
(the same guarantee ``DraftService`` gives in СтройКонтроль).
"""

from __future__ import annotations

import json
from typing import Any

from notimate.packs.location_reports import DraftAlreadyFinalized


class DocumentAlreadyFinalized(DraftAlreadyFinalized):
    pass


def _driver():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg, dict_row


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS documents (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    sender_id TEXT,
    period TEXT,
    seq INTEGER,
    doc_number TEXT,
    doc_type TEXT NOT NULL DEFAULT 'other',
    seller TEXT,
    tax_id TEXT,
    doc_ref TEXT,
    doc_date DATE,
    subtotal NUMERIC,
    vat NUMERIC,
    total NUMERIC,
    currency TEXT,
    payment_method TEXT,
    confidence NUMERIC,
    note TEXT,
    image_sha256 TEXT,
    storage_ref TEXT,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'confirmed', 'rejected')),
    version INTEGER NOT NULL DEFAULT 1,
    confirmed_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_at TIMESTAMPTZ,
    UNIQUE (tenant_id, source_event_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS documents_number_idx ON documents (tenant_id, doc_number) WHERE doc_number IS NOT NULL;
CREATE INDEX IF NOT EXISTS documents_tenant_period_idx ON documents (tenant_id, period, status);
CREATE INDEX IF NOT EXISTS documents_hash_idx ON documents (tenant_id, image_sha256);

CREATE TABLE IF NOT EXISTS document_counters (
    tenant_id TEXT NOT NULL,
    period TEXT NOT NULL,
    last_seq INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, period)
);

CREATE TABLE IF NOT EXISTS document_questions (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    period TEXT,
    doc_number TEXT,
    author_id TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'answered')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS document_questions_tenant_idx ON document_questions (tenant_id, status);

CREATE TABLE IF NOT EXISTS document_periods (
    tenant_id TEXT NOT NULL,
    period TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'sent' CHECK (status IN ('sent', 'accepted')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, period)
);
"""


def period_of(doc_date: str | None, fallback_date: str) -> str:
    return (doc_date or fallback_date)[:7]


class PostgresDocumentsStore:
    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError('DATABASE_URL is required')
        self.database_url = database_url

    def initialize(self) -> None:
        with _driver()[0].connect(self.database_url) as conn:
            conn.execute(SCHEMA_SQL)

    # ── documents ──
    def find_by_hash(self, tenant_id: str, sha256: str) -> dict[str, Any] | None:
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE tenant_id = %s AND image_sha256 = %s AND status <> 'rejected' ORDER BY id LIMIT 1",
                (tenant_id, sha256),
            ).fetchone()
        return dict(row) if row else None

    def find_by_event(self, tenant_id: str, source_event_id: str) -> dict[str, Any] | None:
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            row = conn.execute(
                'SELECT * FROM documents WHERE tenant_id = %s AND source_event_id = %s', (tenant_id, source_event_id)
            ).fetchone()
        return dict(row) if row else None

    def create_document(
        self, tenant_id: str, source_event_id: str, sender_id: str, fields: dict[str, Any],
        image_sha256: str, storage_ref: str, fallback_date: str,
    ) -> tuple[int, bool]:
        """Insert a draft; returns ``(id, created)``. A retried event returns the existing
        row with ``created=False`` so the worker retry never produces a second document."""
        period = period_of(fields.get('doc_date'), fallback_date)
        with _driver()[0].connect(self.database_url) as conn:
            row = conn.execute(
                """
                INSERT INTO documents (
                    tenant_id, source_event_id, sender_id, period, doc_type, seller, tax_id, doc_ref,
                    doc_date, subtotal, vat, total, currency, payment_method, confidence, note,
                    image_sha256, storage_ref
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, source_event_id) DO NOTHING
                RETURNING id
                """,
                (
                    tenant_id, source_event_id, sender_id, period, fields.get('doc_type'), fields.get('seller'),
                    fields.get('tax_id'), fields.get('doc_ref'), fields.get('doc_date'), fields.get('subtotal'),
                    fields.get('vat'), fields.get('total'), fields.get('currency'), fields.get('payment_method'),
                    fields.get('confidence'), fields.get('note'), image_sha256, storage_ref,
                ),
            ).fetchone()
            if row:
                return row[0], True
            existing = conn.execute(
                'SELECT id FROM documents WHERE tenant_id = %s AND source_event_id = %s', (tenant_id, source_event_id)
            ).fetchone()
        return existing[0], False

    def get_document(self, document_id: int) -> dict[str, Any] | None:
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            row = conn.execute('SELECT * FROM documents WHERE id = %s', (document_id,)).fetchone()
        return dict(row) if row else None

    def confirm_document(self, document_id: int, confirmed_by: str = '') -> dict[str, Any]:
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            with conn.transaction():
                doc = conn.execute('SELECT * FROM documents WHERE id = %s FOR UPDATE', (document_id,)).fetchone()
                if doc is None:
                    raise KeyError(f'Document {document_id} was not found')
                if doc['status'] != 'draft':
                    raise DocumentAlreadyFinalized(f'Document {document_id} was already finalized')
                conn.execute(
                    'INSERT INTO document_counters (tenant_id, period) VALUES (%s, %s) ON CONFLICT DO NOTHING',
                    (doc['tenant_id'], doc['period']),
                )
                seq = conn.execute(
                    'UPDATE document_counters SET last_seq = last_seq + 1 WHERE tenant_id = %s AND period = %s RETURNING last_seq',
                    (doc['tenant_id'], doc['period']),
                ).fetchone()['last_seq']
                number = f"{doc['period']}-{seq:03d}"
                updated = conn.execute(
                    """
                    UPDATE documents SET status = 'confirmed', seq = %s, doc_number = %s,
                        confirmed_by = %s, confirmed_at = NOW()
                    WHERE id = %s RETURNING *
                    """,
                    (seq, number, confirmed_by, document_id),
                ).fetchone()
        return dict(updated)

    def reject_document(self, document_id: int) -> dict[str, Any]:
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            with conn.transaction():
                doc = conn.execute('SELECT * FROM documents WHERE id = %s FOR UPDATE', (document_id,)).fetchone()
                if doc is None:
                    raise KeyError(f'Document {document_id} was not found')
                if doc['status'] != 'draft':
                    raise DocumentAlreadyFinalized(f'Document {document_id} was already finalized')
                conn.execute("UPDATE documents SET status = 'rejected' WHERE id = %s", (document_id,))
        return dict(doc)

    def list_confirmed(self, tenant_id: str, period: str) -> list[dict[str, Any]]:
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            rows = conn.execute(
                "SELECT * FROM documents WHERE tenant_id = %s AND period = %s AND status = 'confirmed' ORDER BY seq",
                (tenant_id, period),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_operations(self, tenant_id: str, period: str) -> list[dict[str, Any]]:
        """Read-only view of the Этап 4 ledger for one month (expense-like rows matter for checks)."""
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            rows = conn.execute(
                """
                SELECT id, operation_type, occurred_on, amount, counterparty, description, details
                FROM operations
                WHERE tenant_id = %s AND to_char(occurred_on, 'YYYY-MM') = %s AND status = 'confirmed'
                ORDER BY occurred_on, id
                """,
                (tenant_id, period),
            ).fetchall()
        return [dict(row) for row in rows]

    # ── accountant questions and period acceptance ──
    def add_question(self, tenant_id: str, author_id: str, text: str, period: str | None = None, doc_number: str | None = None) -> int:
        with _driver()[0].connect(self.database_url) as conn:
            return conn.execute(
                'INSERT INTO document_questions (tenant_id, period, doc_number, author_id, text) VALUES (%s,%s,%s,%s,%s) RETURNING id',
                (tenant_id, period, doc_number, author_id, text),
            ).fetchone()[0]

    def open_questions(self, tenant_id: str, period: str | None = None) -> list[dict[str, Any]]:
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            rows = conn.execute(
                """
                SELECT * FROM document_questions
                WHERE tenant_id = %s AND status = 'open' AND (%s::text IS NULL OR period = %s)
                ORDER BY id
                """,
                (tenant_id, period, period),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_period(self, tenant_id: str, period: str, status: str) -> None:
        with _driver()[0].connect(self.database_url) as conn:
            conn.execute(
                """
                INSERT INTO document_periods (tenant_id, period, status) VALUES (%s, %s, %s)
                ON CONFLICT (tenant_id, period) DO UPDATE SET status = EXCLUDED.status, updated_at = NOW()
                """,
                (tenant_id, period, status),
            )

    def period_status(self, tenant_id: str, period: str) -> str | None:
        with _driver()[0].connect(self.database_url) as conn:
            row = conn.execute(
                'SELECT status FROM document_periods WHERE tenant_id = %s AND period = %s', (tenant_id, period)
            ).fetchone()
        return row[0] if row else None
