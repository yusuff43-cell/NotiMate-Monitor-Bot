"""Shared helpers for the anonymized fixture suite (offline tests and the live eval)."""

from __future__ import annotations

import json
import re
from pathlib import Path

FIXTURES_DIR = Path(__file__).with_name('fixtures')
REAL_PATTERNS_PATH = FIXTURES_DIR / 'real_patterns.json'


def load_scenarios(path: Path = REAL_PATTERNS_PATH) -> list[dict]:
    return json.loads(path.read_text(encoding='utf-8'))['scenarios']


def classify_model_output(channel: str, output: str | None) -> str:
    """Name what the app will do with a raw model answer.

    Mirrors the routing in ``app.process_line_event``: text answers may be IGNORE or a
    problem alert, image answers may be NOT_FINANCE, everything else is JSON whose
    ``type`` (text) or ``doc_type`` (image) selects the projection.  Unparseable answers
    are reported as ``malformed`` / ``missing_type`` because the app raises on them so the
    worker can retry.
    """
    output = (output or '').strip()
    if channel == 'text':
        if not output or output == 'IGNORE':
            return 'ignore'
        if output.startswith('ВАЖНО [ПРОБЛЕМА]'):
            return 'problem'
    elif 'NOT_FINANCE' in output:
        return 'not_finance'
    match = re.search(r'\{.*\}', output, re.DOTALL)
    if not match:
        return 'unparsed'
    try:
        data = json.loads(match.group())
    except ValueError:
        return 'malformed'
    key = 'type' if channel == 'text' else 'doc_type'
    return str(data.get(key) or 'missing_type')
