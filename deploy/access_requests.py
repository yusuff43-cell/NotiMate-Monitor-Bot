#!/usr/bin/env python3
"""Review and decide WhatsApp access requests (run by the developer).

    docker compose -f compose.vps.yml exec -T worker python deploy/access_requests.py list
    docker compose -f compose.vps.yml exec -T worker python deploy/access_requests.py approve 7 --role staff --tenant erzhan-cafe   # shared number: name the business
    docker compose -f compose.vps.yml exec -T worker python deploy/access_requests.py approve 8 --role staff --location erzhan-loc-1 --name "Аня"
    docker compose -f compose.vps.yml exec -T worker python deploy/access_requests.py approve 9 --role accountant
    docker compose -f compose.vps.yml exec -T worker python deploy/access_requests.py reject 10

Roles: ``staff`` (may send reports/documents; for «Отчёты точек» also give ``--location``),
``owner`` (owner commands and notifications), ``accountant`` (package, «принято»/«вопрос»).
The requester is told the result in WhatsApp (they wrote a moment ago, so the reply is inside
WhatsApp's 24-hour window). Reads DATABASE_URL and WHATSAPP_SECRETS_JSON from the environment.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from notimate.access import ROLES, PostgresAccessStore, apply_approval  # noqa: E402
from notimate.channels.whatsapp import send_text  # noqa: E402
from notimate.packs.location_reports import PostgresLocationReportsStore  # noqa: E402
from notimate.tenant_store import PostgresTenantStore  # noqa: E402
from notimate.tenants import whatsapp_channel_config  # noqa: E402

ROLE_LABEL = {'staff': 'сотрудник', 'owner': 'владелец', 'accountant': 'бухгалтер'}


def notify(tenant_store, request, text: str) -> bool:
    row = tenant_store.find_channel(request['channel'], request['routing_key'])
    secrets = json.loads(os.environ.get('WHATSAPP_SECRETS_JSON') or '{}')
    config = whatsapp_channel_config(row, secrets.get(row['channel']['secret_ref'])) if row else None
    if not config:
        shared = tenant_store.shared_number(request['routing_key'])
        token = (secrets.get(shared['secret_ref']) or {}).get('access_token') if shared else None
        config = {'access_token': token, 'phone_number_id': request['routing_key']} if token else None
    if not config:
        return False
    try:
        send_text(config['access_token'], config['phone_number_id'], request['sender_id'], text)
        return True
    except Exception as exc:
        print(f'warning: could not message the requester ({exc})', file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list')
    approve = sub.add_parser('approve')
    approve.add_argument('id', type=int)
    approve.add_argument('--role', choices=ROLES, default='staff')
    approve.add_argument('--tenant', help='Business id — required on a shared number (see «клиенты» / list)')
    approve.add_argument('--location')
    approve.add_argument('--name', default='')
    reject = sub.add_parser('reject')
    reject.add_argument('id', type=int)
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        sys.exit('DATABASE_URL is required')
    access = PostgresAccessStore(database_url)
    access.initialize()
    tenants = PostgresTenantStore(database_url)
    tenants.initialize()

    if args.cmd == 'list':
        pending = access.list_pending()
        if not pending:
            print('No pending requests.')
        for req in pending:
            row = tenants.find_channel(req['channel'], req['routing_key'])
            shared = tenants.shared_number(req['routing_key']) if not row else None
            tenant = (row['tenant'].get('name') or row['tenant']['id']) if row else ('(общий номер — укажите --tenant)' if shared else f"(unknown number {req['routing_key']})")
            print(f"#{req['id']}  {tenant}  +{req['sender_id']}  {req['created_at']:%Y-%m-%d %H:%M}  «{req['message']}»")
        return 0

    request = access.get(args.id)
    if not request or request['status'] != 'pending':
        sys.exit(f'Request {args.id} is not pending')
    if args.cmd == 'reject':
        access.decide(args.id, 'rejected')
        notify(tenants, request, 'К сожалению, доступ не подтверждён. Если это ошибка — свяжитесь с владельцем бизнеса.')
        print(f'Request {args.id} rejected')
        return 0

    reports = PostgresLocationReportsStore(database_url)
    reports.initialize()
    try:
        result = apply_approval(tenants, reports, request, args.role, location_id=args.location, name=args.name, tenant_id=args.tenant)
    except ValueError as exc:
        sys.exit(f'Cannot approve: {exc}')
    access.decide(args.id, 'approved', result['tenant_id'], args.role)
    told = notify(tenants, request, f"✅ Доступ открыт: {result['tenant_name']}, роль — {ROLE_LABEL[args.role]}. Напишите «помощь», чтобы увидеть, что умеет бот.")
    print(f"Request {args.id} approved: +{request['sender_id']} → {result['tenant_name']} as {args.role}" + ('' if told else ' (requester not notified)'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
