#!/usr/bin/env python3
"""Configure WhatsApp's «/» command list and ice breaker prompts for one phone number.

WhatsApp has no persistent docked button bar like LINE's Rich Menu — this is the closest
real equivalent Meta offers: commands (shown when the owner types "/" in the message box)
and ice breakers (shown only before the very first message in a brand new chat). One-time
per-number config, run manually when the command list changes — not called on every message.

    docker compose -f compose.vps.yml exec -T worker \\
        python deploy/configure_whatsapp_commands.py --phone-number-id 1242607492276839 --dry-run
    docker compose -f compose.vps.yml exec -T worker \\
        python deploy/configure_whatsapp_commands.py --phone-number-id 1242607492276839
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notimate.channels.whatsapp import configure_conversational_automation  # noqa: E402

# «Отчёты точек» (Этап 6) default command set. Command names are ASCII-only — Meta's "/"
# picker is a plain slash-command palette, not something to guess at with Cyrillic.
LOCATION_REPORTS_COMMANDS = [
    ('summary', 'Сводка по точкам за сегодня'),
    ('missing', 'Кто ещё не отчитался'),
    ('help', 'Что умеет этот бот'),
]
LOCATION_REPORTS_PROMPTS = ['Сводка по точкам', 'Кто не отчитался']


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phone-number-id', required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    secrets = json.loads(os.environ.get('WHATSAPP_SECRETS_JSON') or '{}')
    secret = secrets.get(args.phone_number_id)
    if not secret or not secret.get('access_token'):
        parser.error(f'No access_token in WHATSAPP_SECRETS_JSON for {args.phone_number_id}')

    if args.dry_run:
        print(f'commands: {LOCATION_REPORTS_COMMANDS}')
        print(f'prompts: {LOCATION_REPORTS_PROMPTS}')
        print('Nothing sent (--dry-run).')
        return 0

    result = configure_conversational_automation(
        secret['access_token'], args.phone_number_id,
        commands=LOCATION_REPORTS_COMMANDS, prompts=LOCATION_REPORTS_PROMPTS,
    )
    print(result)
    return 0


if __name__ == '__main__':
    sys.exit(main())
