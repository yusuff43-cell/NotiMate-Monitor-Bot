#!/usr/bin/env python3
"""Point owner navigation at the Overview worksheet without exposing secrets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    project_dir = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', type=Path, default=project_dir / '.env')
    parser.add_argument('--gid', required=True, help='Numeric Google Sheets gid for the Overview worksheet')
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.gid.isdigit():
        raise SystemExit('gid must contain only digits')
    lines = args.env.read_text(encoding='utf-8').splitlines()
    for index, line in enumerate(lines):
        if not line.startswith('CLIENTS_JSON='):
            continue
        clients = json.loads(line.split('=', 1)[1])
        for client in clients.values():
            client['sheet_gid'] = int(args.gid)
        lines[index] = 'CLIENTS_JSON=' + json.dumps(clients, ensure_ascii=False, separators=(',', ':'))
        args.env.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        print({'clients_updated': len(clients), 'overview_gid': int(args.gid)})
        return
    raise SystemExit('CLIENTS_JSON is missing')


if __name__ == '__main__':
    main()
