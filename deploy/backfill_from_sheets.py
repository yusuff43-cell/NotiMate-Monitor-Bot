#!/usr/bin/env python3
"""Load / re-sync a client's Google Sheets data into the PostgreSQL ledger (on demand).

Thin command-line wrapper over ``notimate.projections.sheets_sync`` — the same code the twice-daily
scheduled sync runs (see its docstring for the exact rules: event-keyed rows are updated in
place, hand-typed rows get content keys, deleted rows are rejected, unsafe mass-deletes are
refused). Read-only on the sheet; ``--dry-run`` writes nothing.

    docker compose -f compose.vps.yml exec -T worker python deploy/backfill_from_sheets.py --tenant <LINE destination> --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['DISABLE_SCHEDULER'] = '1'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--tenant', required=True, help='tenant id (the LINE destination for JSC)')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    import app
    from notimate.projections.sheets_sync import sync_tenant
    cfg = app.find_client(args.tenant)
    if not (cfg and app.gc and app.DATABASE_URL):
        sys.exit('Need a known tenant, Google credentials and DATABASE_URL')
    print(sync_tenant(args.tenant, cfg, database_url=app.DATABASE_URL, gc=app.gc, dry_run=args.dry_run))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
