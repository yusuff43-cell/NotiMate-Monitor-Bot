#!/usr/bin/env python3
"""Import CLIENTS_JSON destinations into the tenants/tenant_channels tables (Этап 2, docs/21).

Idempotent: safe to re-run, upserts by (channel, external_id) for channels and by id for
tenants. Does not touch or duplicate secrets — ``secret_ref`` is set to the CLIENTS_JSON
destination key itself, so ``app.find_client`` keeps reading the real
``channel_access_token``/``channel_secret`` straight out of CLIENTS_JSON via that reference
(see notimate/tenants.py:channel_row_to_client_cfg). Nothing in this script prints a secret.

Run inside the worker container, where CLIENTS_JSON and DATABASE_URL are already set:

    docker compose -f compose.vps.yml exec -T worker \\
        python deploy/import_tenants_from_clients_json.py --dry-run
    docker compose -f compose.vps.yml exec -T worker \\
        python deploy/import_tenants_from_clients_json.py

``--dry-run`` prints what would be written (no secrets — only ids, names, sheet_id, owner
count) without touching the database.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notimate.tenant_store import PostgresTenantStore  # noqa: E402

# LINE clients configured today are all Thailand tenants; a channel this script imports
# from a country-specific CLIENTS_JSON later can override this with --country/--timezone.
DEFAULT_COUNTRY = 'TH'
DEFAULT_TIMEZONE = 'Asia/Bangkok'
DEFAULT_OWNER_LANGUAGE = 'ru'


def build_rows(clients: dict) -> list[tuple[dict, dict]]:
    rows = []
    for destination, cfg in clients.items():
        tenant = {
            'id': destination,
            'name': cfg.get('name') or destination,
            'country': DEFAULT_COUNTRY,
            'timezone': DEFAULT_TIMEZONE,
            'owner_language': DEFAULT_OWNER_LANGUAGE,
            'business_type': cfg.get('business_type'),
            'sheet_id': cfg.get('sheet_id'),
            'custom_context': cfg.get('custom_context'),
            'status': 'active',
        }
        owner_ids = [cfg['owner_line_id']]
        if cfg.get('owner_line_id_2'):
            owner_ids.append(cfg['owner_line_id_2'])
        channel = {
            'tenant_id': destination,
            'channel': 'line',
            'external_id': destination,
            'secret_ref': destination,
            'allowed_chats': cfg.get('allowed_group_ids'),
            'owner_ids': owner_ids,
        }
        rows.append((tenant, channel))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='Print what would change, write nothing')
    parser.add_argument('--destination', help='Import only this CLIENTS_JSON destination')
    args = parser.parse_args()

    clients_json = os.environ.get('CLIENTS_JSON')
    if not clients_json:
        parser.error('CLIENTS_JSON is required')
    clients = json.loads(clients_json)
    if args.destination:
        if args.destination not in clients:
            parser.error(f'{args.destination} is not present in CLIENTS_JSON')
        clients = {args.destination: clients[args.destination]}

    rows = build_rows(clients)

    if args.dry_run:
        for tenant, channel in rows:
            print(
                f"tenant id={tenant['id']!r} name={tenant['name']!r} "
                f"sheet_id={'set' if tenant['sheet_id'] else 'MISSING'} "
                f"owners={len(channel['owner_ids'])} "
                f"allowed_chats={'set' if channel['allowed_chats'] is not None else 'legacy (all groups)'}"
            )
        print(f'{len(rows)} tenant(s) would be imported. Nothing written (--dry-run).')
        return 0

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is required (omit --dry-run only inside a container with DB access)')
    store = PostgresTenantStore(database_url)
    store.initialize()
    for tenant, channel in rows:
        store.upsert_tenant(tenant)
        store.upsert_channel(channel)
    print(f'{len(rows)} tenant(s) imported.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
