#!/usr/bin/env python3
"""Apply one tenant's configuration from a JSON file — the way real client data
(locations, staff, accountant ids, confirmation policy) gets into the system without code
changes or hand-written SQL. Idempotent: running it again with edited data updates rows.

    python deploy/apply_tenant_config.py deploy/tenant_config.example.json --dry-run
    docker compose -f compose.vps.yml exec -T worker python deploy/apply_tenant_config.py /tmp/erzhan.json

The file never contains secrets: a channel's ``secret_ref`` only names an entry of the
server's WHATSAPP_SECRETS_JSON / CLIENTS_JSON. Note ``tenant.modules`` REPLACES the stored
modules object (send the complete object). Reads DATABASE_URL from the environment.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from notimate.packs.location_reports import PostgresLocationReportsStore  # noqa: E402
from notimate.tenant_store import PostgresTenantStore  # noqa: E402

REQUIRED_TENANT = ('id', 'name', 'country', 'timezone')


def validate(config: dict) -> list[str]:
    errors: list[str] = []
    tenant = config.get('tenant')
    if not isinstance(tenant, dict):
        return ['"tenant" object is required']
    errors += [f'tenant.{key} is required' for key in REQUIRED_TENANT if not tenant.get(key)]
    for channel in config.get('channels', []):
        for key in ('channel', 'external_id', 'secret_ref'):
            if not channel.get(key):
                errors.append(f'channels[].{key} is required')
        if channel.get('channel') not in ('line', 'whatsapp', 'telegram'):
            errors.append(f"unknown channel {channel.get('channel')!r}")
    location_ids = {loc.get('id') for loc in config.get('locations', [])}
    for loc in config.get('locations', []):
        if not loc.get('id') or not loc.get('name'):
            errors.append('locations[] need id and name')
    for person in config.get('staff', []):
        if not person.get('sender_id') or not person.get('location_id'):
            errors.append('staff[] need sender_id and location_id')
        elif person['location_id'] not in location_ids:
            errors.append(f"staff {person['sender_id']}: unknown location_id {person['location_id']!r}")
    confirmation = (tenant.get('modules') or {}).get('confirmation', {})
    for event_type, policy in confirmation.items():
        if policy not in ('auto', 'confirm', 'confirm_if_low_confidence'):
            errors.append(f'modules.confirmation.{event_type}: invalid policy {policy!r}')
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('config_file')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    config = json.loads(Path(args.config_file).read_text(encoding='utf-8'))
    errors = validate(config)
    if errors:
        print('\n'.join(f'ERROR: {e}' for e in errors), file=sys.stderr)
        return 2
    tenant = config['tenant']
    print(f"tenant {tenant['id']}: {len(config.get('channels', []))} channel(s), "
          f"{len(config.get('locations', []))} location(s), {len(config.get('staff', []))} staff")
    if args.dry_run:
        print('dry run: nothing written')
        return 0

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        sys.exit('DATABASE_URL is required')
    tenants = PostgresTenantStore(database_url)
    tenants.initialize()
    tenants.upsert_tenant(tenant)
    for channel in config.get('channels', []):
        tenants.upsert_channel({'tenant_id': tenant['id'], 'allowed_chats': None, 'owner_ids': [], **channel})
    reports = PostgresLocationReportsStore(database_url)
    reports.initialize()
    for loc in config.get('locations', []):
        reports.upsert_location(tenant['id'], loc['id'], loc['name'])
    for person in config.get('staff', []):
        reports.upsert_staff(tenant['id'], person['sender_id'], person['location_id'], person.get('name', ''), person.get('role', 'staff'))
    print('applied')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
