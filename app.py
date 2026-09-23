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

from notimate.tenants import channel_row_to_client_cfg, validate_clients
from notimate.tenant_store import PostgresTenantStore
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
from notimate.processing.ai import (  # noqa: E402
    analyze_image,
    analyze_text,
    ask_openai,
    openai_usage_values,
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
from notimate.pipeline import process_line_event  # noqa: E402


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
            scheduler.add_job(evening_summary, 'cron', hour=20, minute=0, args=[cfg], id=f"evening_{bot_id}")
            scheduler.add_job(weekly_report, 'cron', day_of_week='sun', hour=18, minute=0, args=[cfg], id=f"weekly_{bot_id}")
    scheduler.start()
    logger.info('scheduler_started')
except Exception as exc:
    logger.info('scheduler_not_started', extra={'error_type': type(exc).__name__})

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
