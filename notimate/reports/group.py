"""Evening summary for owners of several businesses («Ержан — 3 бизнеса»).

One WhatsApp message per owner covering ONLY the businesses that person owns: each business on
its own line with its own name and currency, then totals per currency (never adding baht to
tenge). Data comes from PostgreSQL (the same ledger as the dashboard). Sent after the per-business
summaries, at 20:30 local time.
"""

from __future__ import annotations

from typing import Any

from logging_utils import get_logger

logger = get_logger()
SYMBOLS = {'KZT': '₸', 'THB': '฿', 'RUB': '₽', 'USD': '$', 'EUR': '€'}


def _money(value: float, currency: str) -> str:
    return f"{value:,.0f}".replace(',', ' ') + ' ' + SYMBOLS.get(currency, currency)


def format_group_summary(group_key: str, date_label: str, lines: list[dict[str, Any]]) -> str:
    """``lines``: ``{'name', 'currency', 'revenue', 'expenses'}`` per business."""
    out = [f'🌙 Сводка по бизнесам «{group_key}» за {date_label}:']
    totals: dict[str, list[float]] = {}
    for line in lines:
        out.append(f"• {line['name']}: выручка {_money(line['revenue'], line['currency'])}, расходы {_money(line['expenses'], line['currency'])}")
        slot = totals.setdefault(line['currency'], [0.0, 0.0])
        slot[0] += line['revenue']
        slot[1] += line['expenses']
    for currency, (revenue, expenses) in sorted(totals.items()):
        out.append(f"Итого {currency}: выручка {_money(revenue, currency)}, расходы {_money(expenses, currency)}, результат {_money(revenue - expenses, currency)}")
    return '\n'.join(out)


def send_group_summaries(timezone_name: str) -> int:
    """Send each multi-business owner their summary; returns how many messages were sent.
    Runs for the groups whose businesses live in ``timezone_name``."""
    import app
    from notimate.timeutil import local_date

    store, reader = app.tenant_store, getattr(app, 'dashboard_reader', None)
    if store is None or reader is None:
        return 0
    sent = 0
    try:
        rows = {r['tenant']['id']: r for r in store.list_channels('whatsapp')}
        for group_key in store.list_groups():
            tenants = store.group_tenants(group_key)
            if not tenants or (tenants[0].get('timezone') or 'Asia/Almaty') != timezone_name:
                continue
            import datetime as dt
            day = dt.date.fromisoformat(local_date(timezone_name))
            for owner in sorted({o for t in tenants for o in t['owner_ids']}):
                mine = [t for t in tenants if owner in t['owner_ids']]
                if len(mine) < 2:
                    continue  # a single business already gets its own evening summary
                lines = []
                for t in mine:
                    totals = reader.daily_totals(t['id'], day, day, t.get('vertical_pack') == 'location_reports').get(day.isoformat(), {})
                    currency = {'KZ': 'KZT', 'TH': 'THB', 'RU': 'RUB'}.get((t.get('country') or '').upper(), 'THB')
                    lines.append({'name': t.get('name') or t['id'], 'currency': (t.get('modules') or {}).get('currency') or currency,
                                  'revenue': totals.get('revenue', 0.0), 'expenses': totals.get('expenses', 0.0)})
                row = next((rows[t['id']] for t in mine if t['id'] in rows), None)
                secret = app.WHATSAPP_SECRETS.get(row['channel']['secret_ref']) if row else None
                if not (row and secret and secret.get('access_token')):
                    continue
                try:
                    app.whatsapp_send_proactive(secret['access_token'], row['channel']['external_id'], owner, format_group_summary(group_key, day.strftime('%d.%m.%Y'), lines))
                    sent += 1
                except Exception as exc:
                    logger.warning('group_summary_send_failed', extra={'error_type': type(exc).__name__})
    except Exception as exc:
        logger.warning('group_summary_failed', extra={'error_type': type(exc).__name__})
    return sent
