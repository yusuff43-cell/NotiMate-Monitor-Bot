#!/usr/bin/env python3
"""Retire the in-sheet «Обзор» tab: HIDE it (never delete) and report the next working tab.

The owner replaced «Обзор» with the external web dashboard. Hiding keeps every cell, chart and
the version history intact — the tab can be restored with ``--unhide`` or deleted by the owner
in Google Sheets in one click once they're sure. The script prints the gid of the next visible
working tab; use it for the Rich Menu button:

    python deploy/set_overview_sheet_gid.py --gid <gid> && python deploy/configure_owner_rich_menu.py

Run inside the worker container (it has GOOGLE_CREDENTIALS and CLIENTS_JSON):

    docker compose -f compose.vps.yml exec -T worker python deploy/retire_overview_sheet.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import gspread
from google.oauth2.service_account import Credentials

TITLE = 'Обзор'


def next_working_gid(worksheets) -> int | None:
    """gid of the first visible tab after «Обзор» (wrapping to the start), skipping «Обзор» itself."""
    ordered = [ws for ws in worksheets if not ws.isSheetHidden]
    titles = [ws.title for ws in ordered]
    if TITLE in titles:
        start = titles.index(TITLE)
        ordered = ordered[start + 1:] + ordered[:start]
    candidates = [ws for ws in ordered if ws.title != TITLE]
    return candidates[0].id if candidates else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--destination', help='Only this CLIENTS_JSON destination')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--unhide', action='store_true', help='Restore the tab instead of hiding it')
    args = parser.parse_args()

    clients = json.loads(os.environ['CLIENTS_JSON'])
    if args.destination:
        clients = {args.destination: clients[args.destination]}
    creds = Credentials.from_service_account_info(json.loads(os.environ['GOOGLE_CREDENTIALS']), scopes=['https://www.googleapis.com/auth/spreadsheets'])
    gc = gspread.authorize(creds)

    for destination, client in clients.items():
        label = client.get('name') or f'…{destination[-6:]}'
        sh = gc.open_by_key(client['sheet_id'])
        worksheets = sh.worksheets()
        print(f'{label}: tabs = ' + ', '.join(f"{ws.title}{' (hidden)' if ws.isSheetHidden else ''}[{ws.id}]" for ws in worksheets))
        overview = next((ws for ws in worksheets if ws.title == TITLE), None)
        if overview is None:
            print(f'{label}: no «{TITLE}» tab — nothing to do')
            continue
        if args.unhide:
            if not args.dry_run:
                overview.show()
            print(f'{label}: «{TITLE}» {"would be shown" if args.dry_run else "shown"}')
            continue
        gid = next_working_gid(worksheets)
        if not args.dry_run and not overview.isSheetHidden:
            overview.hide()
        print(f'{label}: «{TITLE}» {"would be hidden" if args.dry_run else "hidden (not deleted)"}; next working tab gid = {gid}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
