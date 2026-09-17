from __future__ import annotations

import os
import time
from collections.abc import Callable

from event_store import EventJob, PostgresEventStore
from logging_utils import get_logger

MAX_ATTEMPTS = int(os.environ.get('WORKER_MAX_ATTEMPTS', '5'))
POLL_SECONDS = float(os.environ.get('WORKER_POLL_SECONDS', '1'))
logger = get_logger()


def run_once(store, processor: Callable[[str, dict], None]) -> bool:
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
    from app import process_line_event

    logger.info('worker_started')
    while True:
        if not run_once(store, process_line_event):
            time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    main()
