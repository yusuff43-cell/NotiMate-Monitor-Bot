#!/usr/bin/env python3
"""Turn a dedicated WhatsApp number into a SHARED number (one bot number, many businesses).

The number's current business keeps working: its owners, staff list and location employees
become ``tenant_members`` of that business, then the number is registered as shared and the old
one-tenant channel row is removed (its lists moved, nothing else lost). After this, the number
routes every message by the sender (notimate/shared_number.py) and new people join through
access requests.

    python deploy/make_number_shared.py --phone-number-id 1242607492276839 --dry-run
    python deploy/make_number_shared.py --phone-number-id 1242607492276839

Take a database backup first (deploy/backup-postgres.sh). Reads DATABASE_URL from the environment.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from notimate.packs.location_reports import PostgresLocationReportsStore  # noqa: E402
from notimate.tenant_store import PostgresTenantStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--phone-number-id', required=True)
    parser.add_argument('--label', default='NotiMate shared number')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        sys.exit('DATABASE_URL is required')
    tenants = PostgresTenantStore(database_url)
    tenants.initialize()
    row = tenants.find_channel('whatsapp', args.phone_number_id)
    if row is None:
        if tenants.shared_number(args.phone_number_id):
            print('Already a shared number — nothing to do')
            return 0
        sys.exit('No dedicated WhatsApp channel with that phone_number_id')
    tenant, channel = row['tenant'], row['channel']
    owners = [str(o) for o in channel.get('owner_ids') or []]
    staff = [str(a) for a in channel.get('allowed_chats') or []]
    reports = PostgresLocationReportsStore(database_url)
    reports.initialize()
    location_staff = []
    if tenant.get('vertical_pack') == 'location_reports':
        import psycopg
        with psycopg.connect(database_url) as conn:
            location_staff = conn.execute('SELECT sender_id, location_id, name FROM staff WHERE tenant_id = %s', (tenant['id'],)).fetchall()
    print(f"{tenant['id']}: owners={len(owners)} staff={len(staff)} location_staff={len(location_staff)} -> tenant_members")
    if args.dry_run:
        print('dry run: nothing changed')
        return 0

    tenants.mark_shared(args.phone_number_id, channel['secret_ref'], args.label)
    for owner in owners:
        tenants.add_member(tenant['id'], args.phone_number_id, owner, 'owner')
    for sender in staff:
        tenants.add_member(tenant['id'], args.phone_number_id, sender, 'staff')
    for sender_id, location_id, name in location_staff:
        if sender_id not in owners:
            tenants.add_member(tenant['id'], args.phone_number_id, str(sender_id), 'staff', name or '', location_id)
    import psycopg
    with psycopg.connect(database_url) as conn:
        conn.execute("DELETE FROM tenant_channels WHERE channel = 'whatsapp' AND external_id = %s", (args.phone_number_id,))
    print('done: number is shared; the business now lives in tenant_members')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
