"""Privacy-conscious structured logs for NotiMate services."""

from __future__ import annotations

import datetime
import json
import logging
import os


class JsonFormatter(logging.Formatter):
    """Emit operational metadata, never arbitrary exception messages or payloads."""

    safe_fields = ('event_id', 'attempt', 'accepted', 'duplicates', 'document_type', 'error_type')

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            'timestamp': datetime.datetime.now(datetime.UTC).isoformat(),
            'level': record.levelname,
            'event': record.getMessage(),
        }
        for field in self.safe_fields:
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info and 'error_type' not in payload:
            payload['error_type'] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))


def get_logger() -> logging.Logger:
    logger = logging.getLogger('notimate')
    if logger.handlers:
        return logger
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO').upper())
    logger.propagate = False
    return logger
