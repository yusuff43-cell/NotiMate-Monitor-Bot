"""Flask entrypoint: wires config/services and exposes the NotiMate Core routes.

All real logic (AI calls, Sheets projection, reports, LINE adapter, event dispatch) lives
under ``notimate/``. Those modules read shared state (``gc``, ``CLIENTS``, ``openai_client``,
``event_store`` ...) and call sibling functions via ``import app; app.<name>`` rather than
importing each other directly, so tests can substitute any of them by patching this module
(``app_module.gc = FakeGC(...)`` etc.) exactly as before the notimate/ split.
"""

import sys

# `gunicorn app:app` and every test (`importlib.import_module('app')`) import this file
# under the name "app", so notimate/*'s `import app` resolves to this single module.
# Running `python app.py` directly instead loads it as "__main__"; without this alias, the
# first `import app` from a submodule would re-execute this whole file as a second, separate
# "app" module (duplicate services, then a circular-import crash on `notimate.channels.line`
# being re-entered before it finishes defining its own names). This line makes the two names
# point at the same running module either way.
sys.modules.setdefault('app', sys.modules[__name__])

import os
import json
import datetime  # unused directly; notimate/*.py patch app.datetime.datetime in tests via this module
from flask import Flask, request, abort
from linebot.v3.exceptions import InvalidSignatureError
from openai import OpenAI
import gspread
from google.oauth2.service_account import Credentials
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

from notimate.tenants import channel_row_to_client_cfg, validate_clients, whatsapp_channel_config
from notimate.tenant_store import PostgresTenantStore
from notimate.inbound_store import PostgresInboundStore
from notimate.projections.operations_store import PostgresOperationsStore
from notimate.packs.location_reports import PostgresLocationReportsStore, process_location_report_event, send_evening_summary as location_reports_evening_summary
from notimate.packs.accountant.store import PostgresDocumentsStore
from notimate.packs.accountant import flow as accountant_flow
from notimate.dashboard.reader import PostgresDashboardReader
from notimate.dashboard.routes import register_routes as register_dashboard_routes
from event_store import PostgresEventStore
from logging_utils import get_logger

app = Flask(__name__)
logger = get_logger()

# ── Глобальные сервисы ──────────────────────────────────────────
OPENAI_MODEL = os.environ.get('OPENAI_MODEL', 'gpt-5.6-luna')
openai_client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])

if os.environ.get('CLIENTS_JSON'):
    CLIENTS = json.loads(os.environ['CLIENTS_JSON'])
else:
    with open('clients.json', 'r', encoding='utf-8') as f:
        CLIENTS = json.load(f)

CLIENTS = validate_clients(CLIENTS)

DATABASE_URL = os.environ.get('DATABASE_URL', '')
event_store = PostgresEventStore(DATABASE_URL) if DATABASE_URL else None
DB_ENABLED = False
if event_store:
    try:
        event_store.initialize()
        DB_ENABLED = True
    except Exception as exc:
        logger.error('database_init_failed', extra={'error_type': type(exc).__name__})

tenant_store = PostgresTenantStore(DATABASE_URL) if DATABASE_URL else None
TENANTS_DB_ENABLED = False
if tenant_store:
    try:
        tenant_store.initialize()
        TENANTS_DB_ENABLED = True
    except Exception as exc:
        logger.error('tenant_store_init_failed', extra={'error_type': type(exc).__name__})

# WhatsApp Cloud API (Этап 3): credentials bridge, parallel to how CLIENTS_JSON holds LINE
# secrets today. Keyed by an arbitrary secret_ref (tenant_channels.secret_ref), not by
# phone_number_id, so one tenant's ref name stays stable even if its number changes.
whatsapp_inbound_store = PostgresInboundStore(DATABASE_URL, 'whatsapp') if DATABASE_URL else None
WHATSAPP_DB_ENABLED = False
if whatsapp_inbound_store:
    try:
        whatsapp_inbound_store.initialize()
        WHATSAPP_DB_ENABLED = True
    except Exception as exc:
        logger.error('whatsapp_inbound_store_init_failed', extra={'error_type': type(exc).__name__})
# Business event ledger (Этап 4): dual-write alongside Sheets, additive only — reports
# still read Sheets until a week of dual-written data can be compared (see docs/21, docs/05).
operations_store = PostgresOperationsStore(DATABASE_URL) if DATABASE_URL else None
OPERATIONS_DB_ENABLED = False
if operations_store:
    try:
        operations_store.initialize()
        OPERATIONS_DB_ENABLED = True
    except Exception as exc:
        logger.error('operations_store_init_failed', extra={'error_type': type(exc).__name__})

# «Отчёты точек» (Этап 6): locations/staff/drafts for WhatsApp tenants whose
# tenants.vertical_pack == 'location_reports' (see notimate/pipeline.py:process_whatsapp_event).
location_reports_store = PostgresLocationReportsStore(DATABASE_URL) if DATABASE_URL else None
LOCATION_REPORTS_DB_ENABLED = False
if location_reports_store:
    try:
        location_reports_store.initialize()
        LOCATION_REPORTS_DB_ENABLED = True
    except Exception as exc:
        logger.error('location_reports_store_init_failed', extra={'error_type': type(exc).__name__})

# «Бухгалтер» (Этап 7): documents registry, numbering, accountant questions. Originals live
# on the DOCUMENT_STORAGE_DIR volume (notimate/packs/accountant/storage.py).
documents_store = PostgresDocumentsStore(DATABASE_URL) if DATABASE_URL else None
DOCUMENTS_DB_ENABLED = False
if documents_store:
    try:
        documents_store.initialize()
        DOCUMENTS_DB_ENABLED = True
    except Exception as exc:
        logger.error('documents_store_init_failed', extra={'error_type': type(exc).__name__})

# Подробный отчёт (Этап 8): read-only reader for GET /v1/owner-dashboard; the routes stay
# inert (404) until DASHBOARD_LINK_SECRET and DASHBOARD_API_BASE are set in the environment.
dashboard_reader = PostgresDashboardReader(DATABASE_URL) if DATABASE_URL else None

WHATSAPP_SECRETS = json.loads(os.environ['WHATSAPP_SECRETS_JSON']) if os.environ.get('WHATSAPP_SECRETS_JSON') else {}
WHATSAPP_WEBHOOK_VERIFY_TOKEN = os.environ.get('WHATSAPP_WEBHOOK_VERIFY_TOKEN', '')
# One Meta App secret verifies every tenant's webhook traffic (see whatsapp_channel_config's
# docstring) — this is the App Secret from Meta App Dashboard, not any one WABA's token.
WHATSAPP_APP_SECRET = os.environ.get('WHATSAPP_APP_SECRET', '')

SHEETS_ENABLED = False
gc = None
try:
    creds_json = json.loads(os.environ['GOOGLE_CREDENTIALS'])
    scopes = ['https://www.googleapis.com/auth/spreadsheets']
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    gc = gspread.authorize(creds)
    SHEETS_ENABLED = True
except Exception as exc:
    logger.warning('sheets_init_skipped', extra={'error_type': type(exc).__name__})


def find_client(destination: str):
    """Resolve one LINE destination to a client_cfg.

    Prefers the tenants/tenant_channels tables (Этап 2 of docs/21) once a tenant has been
    imported there; CLIENTS_JSON is the fallback through Этап 3, and stays the only source
    for any destination the tenant store doesn't know about or can't validate — so an
    unmigrated or partially-imported tenant keeps working exactly as before this existed.
    """
    if tenant_store is not None and TENANTS_DB_ENABLED:
        try:
            row = tenant_store.find_channel('line', destination)
        except Exception as exc:
            logger.warning('tenant_lookup_failed', extra={'error_type': type(exc).__name__})
            row = None
        if row:
            secret = CLIENTS.get(row['channel']['secret_ref'])
            cfg = channel_row_to_client_cfg(row, secret)
            if cfg:
                return cfg
            logger.warning('tenant_channel_config_incomplete')
    return CLIENTS.get(destination)


def find_whatsapp_channel(phone_number_id: str):
    """Resolve one WhatsApp phone_number_id to its tenant_store row, or None.

    Unlike find_client, there is no CLIENTS_JSON fallback here — WhatsApp tenants exist
    only in tenants/tenant_channels, so an unresolvable phone_number_id is simply unknown.
    """
    if tenant_store is None or not TENANTS_DB_ENABLED:
        return None
    try:
        return tenant_store.find_channel('whatsapp', phone_number_id)
    except Exception as exc:
        logger.warning('tenant_lookup_failed', extra={'error_type': type(exc).__name__})
        return None


# ── Модули NotiMate Core ────────────────────────────────────────
# Every name below is re-exported on this module so tests and deploy/ scripts keep
# addressing them as `app.<name>`, exactly as when they all lived in this one file.
from notimate.channels.line import (  # noqa: E402
    get_line_api,
    get_line_blob_api,
    get_line_clients,
    notify_owner,
    verify_signature,
)
from notimate.channels.whatsapp import (  # noqa: E402
    download_media as whatsapp_download_media,
    send_document as whatsapp_send_document,
    extract_messages as whatsapp_extract_messages,
    send_interactive_buttons as whatsapp_send_interactive_buttons,
    send_text as whatsapp_send_text,
    verify_signature as whatsapp_verify_signature,
    verify_webhook_challenge as whatsapp_verify_webhook_challenge,
)
from notimate.processing.ai import (  # noqa: E402
    analyze_image,
    analyze_text,
    ask_openai,
    openai_usage_values,
)
from notimate.projections.operations_store import (  # noqa: E402
    record_issue_safely,
    record_operation_safely,
    record_reminder_safely,
    record_stock_signal_safely,
)
from notimate.projections.sheets import (  # noqa: E402
    EVENT_ID_HEADER,
    append_rows_once,
    check_price_drift,
    ensure_event_id_column,
    get_last_date,
    get_or_create_sheet,
    refresh_overview,
    refresh_overview_safely,
    save_выручка,
    save_закупки,
    save_зарплаты,
    save_напоминание,
    save_одиночный_остаток,
    save_остатки,
    save_проблемы,
    save_расходы,
    upcoming_reminders,
)
from notimate.reports.summaries import (  # noqa: E402
    detailed_report,
    evening_summary,
    owner_menu,
    reminders_report,
    weekly_report,
)
from notimate.packs.accountant.flow import line_owner_command, process_accountant_event, register_line_document  # noqa: E402
from notimate.pipeline import process_line_event, process_whatsapp_event  # noqa: E402


@app.route("/webhook", methods=['POST'])
def webhook():
    body = request.get_data(as_text=True)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        abort(400)

    destination = payload.get('destination', '')
    client_cfg = find_client(destination)
    if not client_cfg:
        logger.warning('webhook_unknown_destination')
        return 'OK'

    signature = request.headers.get('X-Line-Signature', '')
    try:
        verify_signature(client_cfg, body, signature)
    except InvalidSignatureError:
        abort(400)

    message_events = [event for event in payload.get('events', []) if event.get('type') == 'message']
    if not message_events:
        return 'OK'
    if not event_store or not DB_ENABLED:
        return {'status': 'database_unavailable'}, 503

    try:
        accepted, duplicates = event_store.register_events(destination, message_events)
    except Exception as exc:
        logger.error('event_registration_failed', extra={'error_type': type(exc).__name__})
        return {'status': 'event_registration_failed'}, 503

    logger.info('events_registered', extra={'accepted': accepted, 'duplicates': duplicates})
    return 'OK'


@app.route("/webhook/whatsapp", methods=['GET'])
def whatsapp_webhook_verify():
    challenge = whatsapp_verify_webhook_challenge(
        WHATSAPP_WEBHOOK_VERIFY_TOKEN,
        request.args.get('hub.mode', ''),
        request.args.get('hub.verify_token', ''),
        request.args.get('hub.challenge', ''),
    )
    if challenge is None:
        abort(403)
    return challenge, 200


@app.route("/webhook/whatsapp", methods=['POST'])
def whatsapp_webhook():
    body = request.get_data()
    signature = request.headers.get('X-Hub-Signature-256', '')
    if not (WHATSAPP_APP_SECRET and whatsapp_verify_signature(WHATSAPP_APP_SECRET, body, signature)):
        abort(403)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        abort(400)

    if not whatsapp_inbound_store or not WHATSAPP_DB_ENABLED:
        return {'status': 'database_unavailable'}, 503

    accepted_total = duplicates_total = 0
    for item in whatsapp_extract_messages(payload):
        try:
            accepted, duplicates = whatsapp_inbound_store.register_events(item['phone_number_id'], [item['message']])
        except Exception as exc:
            logger.error('whatsapp_event_registration_failed', extra={'error_type': type(exc).__name__})
            continue
        accepted_total += accepted
        duplicates_total += duplicates

    logger.info('whatsapp_events_registered', extra={'accepted': accepted_total, 'duplicates': duplicates_total})
    return 'OK'


register_dashboard_routes(app)


@app.route("/health", methods=['GET'])
def health():
    return {"status": "ok", "clients": len(CLIENTS), "sheets": SHEETS_ENABLED}, 200


@app.route("/ready", methods=['GET'])
def ready():
    database_ready = bool(event_store and DB_ENABLED and event_store.ping())
    ready_state = bool(CLIENTS) and SHEETS_ENABLED and database_ready
    payload = {
        "status": "ready" if ready_state else "not_ready",
        "clients": len(CLIENTS),
        "sheets": SHEETS_ENABLED,
        "database": database_ready,
    }
    return payload, 200 if ready_state else 503

# ── Планировщик ──────────────────────────────────────────────────
try:
    if os.environ.get('DISABLE_SCHEDULER') == '1':
        raise RuntimeError('Scheduler disabled for this process')
    scheduler = BackgroundScheduler(timezone=pytz.timezone('Asia/Bangkok'))
    for bot_id, cfg in CLIENTS.items():
        if cfg.get('sheet_id') and gc:
            scheduler.add_job(evening_summary, 'cron', hour=20, minute=0, args=[cfg], kwargs={'tenant_id': bot_id}, id=f"evening_{bot_id}")
            scheduler.add_job(weekly_report, 'cron', day_of_week='sun', hour=18, minute=0, args=[cfg], id=f"weekly_{bot_id}")
    # «Отчёты точек» (Этап 6): 20:00 Asia/Almaty digest per WhatsApp tenant running that
    # pack, one job per configured owner. A per-job timezone works alongside the
    # scheduler's own Asia/Bangkok default (APScheduler supports this per trigger).
    if tenant_store is not None and TENANTS_DB_ENABLED:
        try:
            for row in tenant_store.list_channels('whatsapp'):
                if row['tenant'].get('vertical_pack') != 'location_reports':
                    continue
                config = whatsapp_channel_config(row, WHATSAPP_SECRETS.get(row['channel']['secret_ref']))
                if not config:
                    continue
                for owner_id in row['channel']['owner_ids'] or []:
                    scheduler.add_job(
                        location_reports_evening_summary, 'cron', hour=20, minute=0,
                        timezone=pytz.timezone(config.get('timezone') or 'Asia/Almaty'),
                        args=[row['tenant']['id'], config['access_token'], config['phone_number_id'], owner_id, config.get('timezone')],
                        id=f"location_summary_{row['tenant']['id']}_{owner_id}",
                    )
        except Exception as exc:
            logger.warning('location_reports_scheduling_failed', extra={'error_type': type(exc).__name__})
        # «Бухгалтер» (Этап 7): month package on the 1st at 09:00 and a Monday «не хватает»
        # digest, both in the tenant's own timezone.
        try:
            for row in tenant_store.list_channels('whatsapp'):
                tenant = row['tenant']
                if not accountant_flow.module_enabled(tenant):
                    continue
                config = whatsapp_channel_config(row, WHATSAPP_SECRETS.get(row['channel']['secret_ref']))
                if not config:
                    continue
                tz_name = config.get('timezone') or 'Asia/Almaty'
                settings = accountant_flow.module_settings(tenant)
                recipients = list(dict.fromkeys(
                    [str(o) for o in config['owner_ids']] + [str(a) for a in settings.get('accountant_ids') or []]
                ))
                scheduler.add_job(
                    accountant_flow.send_scheduled_package, 'cron', day=1, hour=9, minute=0,
                    timezone=pytz.timezone(tz_name),
                    args=[tenant, config['access_token'], config['phone_number_id'], recipients, tz_name],
                    id=f"accountant_package_{tenant['id']}",
                )
                scheduler.add_job(
                    accountant_flow.send_weekly_missing_digest, 'cron', day_of_week='mon', hour=10, minute=0,
                    timezone=pytz.timezone(tz_name),
                    args=[tenant, config['access_token'], config['phone_number_id'], [str(o) for o in config['owner_ids']], tz_name],
                    id=f"accountant_digest_{tenant['id']}",
                )
        except Exception as exc:
            logger.warning('accountant_scheduling_failed', extra={'error_type': type(exc).__name__})
    scheduler.start()
    logger.info('scheduler_started')
except Exception as exc:
    logger.info('scheduler_not_started', extra={'error_type': type(exc).__name__})

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
