#!/usr/bin/env python3
"""Opt-in live check of the real model against the anonymized text fixtures.

The offline suite (``test_fixture_regression.py``) replaces the model with recorded
answers.  This script closes the other half: it sends each *text* scenario from
``fixtures/real_patterns.json`` to the configured OpenAI model with the production prompt
and compares the routing decision (stock / purchase / problem / ignore ...) with
``expected_type``.  It never runs in CI and never touches PostgreSQL, Google Sheets or LINE.

Cost: 18 short requests.  Only anonymized, hand-written fixture text is sent.

Run inside the worker container so it uses the production model and prompt:

    docker compose -f compose.vps.yml exec -T worker python deploy/eval_live_fixtures.py

Exit code 0 means every scenario matched; 1 means at least one mismatch.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if not os.environ.get('OPENAI_API_KEY'):
    sys.exit('OPENAI_API_KEY is required')

# Keep the eval side-effect free: no scheduler, no usage rows in the pilot statistics,
# no Sheets client, and a neutral client context instead of a real customer's.
os.environ['DISABLE_SCHEDULER'] = '1'
os.environ.pop('DATABASE_URL', None)
os.environ['GOOGLE_CREDENTIALS'] = '{}'
os.environ['CLIENTS_JSON'] = json.dumps({
    'Ueval': {
        'name': 'Eval cafe',
        'business_type': 'cafe',
        'channel_access_token': 'eval',
        'channel_secret': 'eval',
        'owner_line_id': 'Ueval-owner',
        'sheet_id': 'eval',
    }
})

import app  # noqa: E402  (must follow the environment setup above)
from fixture_support import classify_model_output, load_scenarios  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--id', action='append', help='Run only this scenario id (repeatable)')
    parser.add_argument('--show-output', action='store_true', help='Print the raw model answer for mismatches')
    args = parser.parse_args()

    client = app.CLIENTS['Ueval']
    # offline_only scenarios describe deliberately broken model answers; a healthy model
    # never produces them, so they only make sense in the offline suite.
    scenarios = [s for s in load_scenarios() if s['channel'] == 'text' and not s.get('offline_only')]
    if args.id:
        scenarios = [s for s in scenarios if s['id'] in set(args.id)]
    if not scenarios:
        print('No matching text scenarios.', file=sys.stderr)
        return 2

    mismatches = 0
    print(f'model={app.OPENAI_MODEL} scenarios={len(scenarios)}')
    for scenario in scenarios:
        try:
            answer = app.analyze_text(scenario['message'], client)
        except Exception as exc:  # network, quota, empty answer
            print(f"ERROR   {scenario['id']}: {type(exc).__name__}")
            mismatches += 1
            continue
        actual = classify_model_output('text', answer)
        expected = scenario['expected_type']
        ok = actual == expected
        mismatches += 0 if ok else 1
        print(f"{'OK     ' if ok else 'MISMATCH'} {scenario['id']} [{scenario['language']}] expected={expected} actual={actual}")
        if not ok and args.show_output:
            print('    raw answer:', answer.replace('\n', ' | ')[:300])

    total = len(scenarios)
    print(f'{total - mismatches}/{total} matched')
    return 0 if mismatches == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
