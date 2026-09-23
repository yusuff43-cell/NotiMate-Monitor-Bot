"""«Отчёты точек» — Этап 6 of docs/21: daily location reports over WhatsApp for Ержан.

Draft → confirm/cancel pattern ported from
`Клиенты/Ержан — СтройКонтроль/src/stroycontrol/services.py` (DraftService,
DraftAlreadyFinalized): a draft is only ever finalized once — confirming or cancelling it
marks its status so a repeat button tap (double network delivery, impatient double-tap)
raises DraftAlreadyFinalized instead of creating a second report.

Report fields follow docs/21's own list verbatim (owner hasn't handed over Ержан's actual
notebook yet — placeholder locations are seeded until real names/staff arrive):
дата, точка, выручка, наличные, безнал, внешние выплаты, остаток наличных,
комментарий/проблема. Photo reports are not implemented yet (docs/21 says "текстом или
фото"); text-only is this pass's scope, photo is a near-term follow-up.
"""

from __future__ import annotations

import json
import re
import secrets
from typing import Any


class DraftAlreadyFinalized(ValueError):
    """A confirm/cancel action may only settle one draft once."""


def _driver():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg, dict_row


SCHEMA_SQL = """
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
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'confirmed', 'cancelled')),
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
"""

REPORT_FIELDS = ('revenue', 'cash', 'non_cash', 'external_payouts', 'cash_balance', 'comment')


class PostgresLocationReportsStore:
    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError('DATABASE_URL is required')
        self.database_url = database_url

    def initialize(self) -> None:
        with _driver()[0].connect(self.database_url) as conn:
            conn.execute(SCHEMA_SQL)

    def upsert_location(self, tenant_id: str, location_id: str, name: str) -> None:
        with _driver()[0].connect(self.database_url) as conn:
            conn.execute(
                """
                INSERT INTO locations (id, tenant_id, name)
                VALUES (%s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name
                """,
                (location_id, tenant_id, name),
            )

    def upsert_staff(self, tenant_id: str, sender_id: str, location_id: str, name: str = '', role: str = 'staff') -> None:
        with _driver()[0].connect(self.database_url) as conn:
            conn.execute(
                """
                INSERT INTO staff (tenant_id, sender_id, location_id, name, role)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, sender_id) DO UPDATE SET
                    location_id = EXCLUDED.location_id, name = EXCLUDED.name, role = EXCLUDED.role
                """,
                (tenant_id, sender_id, location_id, name, role),
            )

    def find_staff(self, tenant_id: str, sender_id: str) -> dict[str, Any] | None:
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            row = conn.execute(
                """
                SELECT s.sender_id, s.location_id, s.name, s.role, l.name AS location_name
                FROM staff s JOIN locations l ON l.id = s.location_id
                WHERE s.tenant_id = %s AND s.sender_id = %s AND l.status = 'active'
                """,
                (tenant_id, sender_id),
            ).fetchone()
        return dict(row) if row else None

    def list_locations(self, tenant_id: str) -> list[dict[str, Any]]:
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            rows = conn.execute(
                "SELECT id, name FROM locations WHERE tenant_id = %s AND status = 'active' ORDER BY name",
                (tenant_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_draft(self, tenant_id: str, location_id: str, sender_id: str, occurred_on: str, fields: dict[str, Any], source_text: str) -> str:
        draft_id = secrets.token_hex(6)
        with _driver()[0].connect(self.database_url) as conn:
            conn.execute(
                """
                INSERT INTO location_report_drafts (
                    id, tenant_id, location_id, sender_id, occurred_on,
                    revenue, cash, non_cash, external_payouts, cash_balance, comment, source_text
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    draft_id, tenant_id, location_id, sender_id, occurred_on,
                    fields.get('revenue'), fields.get('cash'), fields.get('non_cash'),
                    fields.get('external_payouts'), fields.get('cash_balance'), fields.get('comment', ''),
                    source_text,
                ),
            )
        return draft_id

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            row = conn.execute("SELECT * FROM location_report_drafts WHERE id = %s", (draft_id,)).fetchone()
        return dict(row) if row else None

    def confirm_draft(self, draft_id: str) -> dict[str, Any]:
        """Finalize a draft into a confirmed location_reports row. Raises KeyError if the
        draft doesn't exist, DraftAlreadyFinalized if it was already confirmed/cancelled —
        so a duplicate button tap can never create a second report."""
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            with conn.transaction():
                draft = conn.execute(
                    "SELECT * FROM location_report_drafts WHERE id = %s FOR UPDATE", (draft_id,)
                ).fetchone()
                if draft is None:
                    raise KeyError(f'Draft {draft_id} was not found')
                if draft['status'] != 'draft':
                    raise DraftAlreadyFinalized(f'Draft {draft_id} was already finalized')
                conn.execute(
                    """
                    INSERT INTO location_reports (
                        tenant_id, location_id, sender_id, occurred_on,
                        revenue, cash, non_cash, external_payouts, cash_balance, comment
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        draft['tenant_id'], draft['location_id'], draft['sender_id'], draft['occurred_on'],
                        draft['revenue'], draft['cash'], draft['non_cash'], draft['external_payouts'],
                        draft['cash_balance'], draft['comment'],
                    ),
                )
                conn.execute("UPDATE location_report_drafts SET status = 'confirmed' WHERE id = %s", (draft_id,))
        return dict(draft)

    def cancel_draft(self, draft_id: str) -> dict[str, Any]:
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            with conn.transaction():
                draft = conn.execute(
                    "SELECT * FROM location_report_drafts WHERE id = %s FOR UPDATE", (draft_id,)
                ).fetchone()
                if draft is None:
                    raise KeyError(f'Draft {draft_id} was not found')
                if draft['status'] != 'draft':
                    raise DraftAlreadyFinalized(f'Draft {draft_id} was already finalized')
                conn.execute("UPDATE location_report_drafts SET status = 'cancelled' WHERE id = %s", (draft_id,))
        return dict(draft)

    def reported_location_ids(self, tenant_id: str, occurred_on: str) -> set[str]:
        with _driver()[0].connect(self.database_url) as conn:
            rows = conn.execute(
                "SELECT DISTINCT location_id FROM location_reports WHERE tenant_id = %s AND occurred_on = %s",
                (tenant_id, occurred_on),
            ).fetchall()
        return {row[0] for row in rows}

    def reports_for_date(self, tenant_id: str, occurred_on: str) -> list[dict[str, Any]]:
        with _driver()[0].connect(self.database_url, row_factory=_driver()[1]) as conn:
            rows = conn.execute(
                """
                SELECT r.*, l.name AS location_name
                FROM location_reports r JOIN locations l ON l.id = r.location_id
                WHERE r.tenant_id = %s AND r.occurred_on = %s
                ORDER BY l.name
                """,
                (tenant_id, occurred_on),
            ).fetchall()
        return [dict(row) for row in rows]


# ── Разбор отчёта из текста ─────────────────────────────────────────────────────────────

REPORT_PROMPT = """Ты извлекаешь данные ежедневного отчёта точки продаж для владельца бизнеса в Казахстане. Входное сообщение может быть на русском или казахском — понимай оба языка, но комментарий возвращай на русском.

Извлеки поля: выручка за день, наличные, безналичные (карта/переводы), внешние выплаты (расходы наличными из кассы: зарплата, закуп и т.п.), остаток наличных в кассе на конец дня, комментарий или проблема (если есть).

Верни ТОЛЬКО JSON: {"revenue":число или null,"cash":число или null,"non_cash":число или null,"external_payouts":число или null,"cash_balance":число или null,"comment":"текст на русском или пустая строка"}

Если сообщение явно не является отчётом по точке (приветствие, вопрос, посторонний текст) — верни только: IGNORE"""


def analyze_report_text(text: str) -> dict[str, Any] | None:
    """Call the model to extract report fields from free text, or None if not a report."""
    import app
    result = app.ask_openai(REPORT_PROMPT, text, 800)
    if not result or result.strip() == 'IGNORE':
        return None
    match = re.search(r'\{.*\}', result, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except ValueError:
        return None
    return {field: data.get(field) for field in REPORT_FIELDS}


def format_draft_message(location_name: str, fields: dict[str, Any]) -> str:
    lines = [f'📋 Отчёт «{location_name}» — проверьте перед сохранением:']
    labels = {
        'revenue': 'Выручка', 'cash': 'Наличные', 'non_cash': 'Безнал',
        'external_payouts': 'Внешние выплаты', 'cash_balance': 'Остаток наличных',
    }
    for field, label in labels.items():
        value = fields.get(field)
        if value is not None:
            lines.append(f'{label}: {value}')
    if fields.get('comment'):
        lines.append(f'Комментарий: {fields["comment"]}')
    return '\n'.join(lines)


def format_confirmed_message(location_name: str) -> str:
    return f'✅ Отчёт «{location_name}» сохранён.'


def format_summary(reports: list[dict[str, Any]], all_locations: list[dict[str, Any]], date_label: str) -> str:
    if not reports:
        return f'🌙 Сводка за {date_label}: отчётов пока нет.'
    total_revenue = sum(float(r['revenue']) for r in reports if r.get('revenue') is not None)
    total_cash_balance = sum(float(r['cash_balance']) for r in reports if r.get('cash_balance') is not None)
    lines = [f'🌙 Сводка за {date_label} ({len(reports)}/{len(all_locations)} точек):', f'Выручка всего: {total_revenue:,.0f}']
    for report in reports:
        lines.append(f"• {report['location_name']}: выручка {report.get('revenue') or 0}, остаток нал. {report.get('cash_balance') or 0}")
    reported_ids = {r['location_id'] for r in reports}
    missing = [loc['name'] for loc in all_locations if loc['id'] not in reported_ids]
    if missing:
        lines.append('🔴 Не отчитались: ' + ', '.join(missing))
    else:
        lines.append(f'Итого остаток наличных: {total_cash_balance:,.0f}')
    return '\n'.join(lines)


def format_missing_report(all_locations: list[dict[str, Any]], reported_ids: set[str]) -> str:
    missing = [loc['name'] for loc in all_locations if loc['id'] not in reported_ids]
    if not missing:
        return '✅ Все точки уже отчитались сегодня.'
    return '🔴 Ещё не отчитались: ' + ', '.join(missing)


OWNER_SUMMARY_COMMANDS = ('сводка', 'свод', 'summary')
OWNER_MISSING_COMMANDS = ('кто не отчитался', 'не отчитались', 'кто не сдал')


def process_location_report_event(row: dict[str, Any], config: dict[str, Any], inbound) -> None:
    """Dispatch one inbound WhatsApp message for a location_reports-pack tenant.

    Button replies (report:confirm:<id> / report:cancel:<id>) settle a draft; an owner
    sender gets СВОДКА / «кто не отчитался»; anyone else registered as staff gets the
    draft → confirm/cancel flow; an unregistered sender is told to contact the owner.
    """
    import app
    from notimate.timeutil import almaty_date

    tenant_id = row['tenant']['id']
    store = app.location_reports_store
    text = inbound.text.strip()
    access_token, phone_number_id = config['access_token'], config['phone_number_id']

    def reply(body: str) -> None:
        app.whatsapp_send_text(access_token, phone_number_id, inbound.sender_id, body)

    if not store or not app.LOCATION_REPORTS_DB_ENABLED:
        reply('Модуль отчётов временно недоступен.')
        return

    if text.startswith('report:edit:'):
        draft_id = text.split(':', 2)[2]
        try:
            store.cancel_draft(draft_id)
        except (KeyError, DraftAlreadyFinalized):
            pass  # already gone either way — we only need the user to resend, not this draft
        reply('Отправьте исправленный отчёт целиком — я создам новый черновик.')
        return

    if text.startswith('report:confirm:') or text.startswith('report:cancel:'):
        draft_id = text.split(':', 2)[2]
        action = store.confirm_draft if text.startswith('report:confirm:') else store.cancel_draft
        try:
            draft = action(draft_id)
        except KeyError:
            reply('Черновик уже не найден. Отправьте отчёт ещё раз.')
            return
        except DraftAlreadyFinalized:
            reply('Этот отчёт уже обработан.')
            return
        if text.startswith('report:confirm:'):
            location = store.find_staff(tenant_id, draft['sender_id'])
            reply(format_confirmed_message(location['location_name'] if location else draft['location_id']))
        else:
            reply('Отчёт отменён. Отправьте исправленный вариант.')
        return

    if inbound.sender_role == 'owner':
        lowered = text.lower()
        if lowered in OWNER_SUMMARY_COMMANDS:
            today = almaty_date()
            reports = store.reports_for_date(tenant_id, today)
            locations = store.list_locations(tenant_id)
            reply(format_summary(reports, locations, today))
            return
        if lowered in OWNER_MISSING_COMMANDS:
            today = almaty_date()
            locations = store.list_locations(tenant_id)
            reported = store.reported_location_ids(tenant_id, today)
            reply(format_missing_report(locations, reported))
            return

    staff = store.find_staff(tenant_id, inbound.sender_id)
    if not staff:
        reply('Доступ к отчётам не настроен для этого номера. Обратитесь к владельцу.')
        return

    fields = analyze_report_text(text)
    if fields is None:
        reply('Не смог распознать отчёт. Укажите выручку, наличные, безнал и остаток наличных.')
        return

    draft_id = store.create_draft(tenant_id, staff['location_id'], inbound.sender_id, almaty_date(), fields, text)
    app.whatsapp_send_interactive_buttons(
        access_token, phone_number_id, inbound.sender_id,
        format_draft_message(staff['location_name'], fields),
        [
            (f'report:confirm:{draft_id}', 'Сохранить'),
            (f'report:edit:{draft_id}', 'Изменить'),
            (f'report:cancel:{draft_id}', 'Отмена'),
        ],
    )


def send_evening_summary(tenant_id: str, access_token: str, phone_number_id: str, owner_id: str) -> None:
    """Scheduled 20:00 Asia/Almaty digest across every location — owner_menu-style job,
    registered once per (tenant, owner) in app.py's scheduler alongside the LINE jobs."""
    import app
    from notimate.timeutil import almaty_date

    store = app.location_reports_store
    if not store:
        return
    try:
        today = almaty_date()
        reports = store.reports_for_date(tenant_id, today)
        locations = store.list_locations(tenant_id)
        app.whatsapp_send_text(access_token, phone_number_id, owner_id, format_summary(reports, locations, today))
    except Exception as exc:
        from logging_utils import get_logger
        get_logger().warning('location_reports_evening_summary_failed', extra={'error_type': type(exc).__name__})
