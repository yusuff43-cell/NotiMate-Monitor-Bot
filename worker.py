from __future__ import annotations

import os
import time
from collections.abc import Callable

from event_store import EventJob, PostgresEventStore
from logging_utils import get_logger

MAX_ATTEMPTS = int(os.environ.get('WORKER_MAX_ATTEMPTS', '5'))
POLL_SECONDS = float(os.environ.get('WORKER_POLL_SECONDS', '1'))
RAW_EVENT_RETENTION_DAYS = int(os.environ.get('RAW_EVENT_RETENTION_DAYS', '14'))
EVENT_LEDGER_RETENTION_DAYS = int(os.environ.get('EVENT_LEDGER_RETENTION_DAYS', '90'))
RETENTION_CLEANUP_SECONDS = float(os.environ.get('RETENTION_CLEANUP_SECONDS', '86400'))
logger = get_logger()


def run_once(store, processor: Callable[[str, dict], None], on_failed: Callable[[EventJob, Exception], None] | None = None) -> bool:
    job: EventJob | None = store.claim_next()
    if job is None:
        return False

    try:
        processor(job.destination, job.payload)
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
        logger.exception('event_failed', extra={'event_id': job.webhook_event_id, 'attempt': job.attempts, 'error_type': type(exc).__name__})
        if job.attempts >= MAX_ATTEMPTS:
            store.mark_failed(job.webhook_event_id, error)
            if on_failed is not None:
                try:
                    on_failed(job, exc)
                except Exception:
                    logger.warning('failure_alert_failed')
        else:
            store.mark_retry(job.webhook_event_id, error, job.attempts)
    else:
        store.mark_completed(job.webhook_event_id)
        logger.info('event_completed', extra={'event_id': job.webhook_event_id, 'attempt': job.attempts})
    return True


def main() -> None:
    database_url = os.environ['DATABASE_URL']
    store = PostgresEventStore(database_url)
    store.initialize()

    os.environ['DISABLE_SCHEDULER'] = '1'
    import app
    from app import process_line_event, process_whatsapp_event
    from notimate.operator import alert_operator

    # WhatsApp reuses run_once against its own inbound_events queue (notimate/inbound_store.py)
    # so the LINE claim/process/retry loop above stays completely unchanged; app.py already
    # constructed and initialized whatsapp_inbound_store, or left it None if DATABASE_URL
    # somehow disappeared between the two initializations (it can't in practice — same env).
    whatsapp_store = app.whatsapp_inbound_store

    logger.info('worker_started')
    next_retention_cleanup = 0.0
    while True:
        now = time.monotonic()
        if now >= next_retention_cleanup:
            try:
                wiped, deleted = store.purge_expired_event_data(
                    RAW_EVENT_RETENTION_DAYS, EVENT_LEDGER_RETENTION_DAYS
                )
                logger.info(
                    'retention_cleanup_completed',
                    extra={'payloads_purged': wiped, 'events_deleted': deleted},
                )
            except Exception as exc:
                logger.warning('retention_cleanup_failed', extra={'error_type': type(exc).__name__})
            if whatsapp_store:
                try:
                    whatsapp_store.purge_expired_event_data(RAW_EVENT_RETENTION_DAYS, EVENT_LEDGER_RETENTION_DAYS)
                except Exception as exc:
                    logger.warning('whatsapp_retention_cleanup_failed', extra={'error_type': type(exc).__name__})
            next_retention_cleanup = now + RETENTION_CLEANUP_SECONDS
        processed = run_once(store, process_line_event, alert_operator)
        if whatsapp_store and run_once(whatsapp_store, process_whatsapp_event, alert_operator):
            processed = True
        if not processed:
            time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    main()
