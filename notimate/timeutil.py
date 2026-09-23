"""Client-local time helpers.

NotiMate Core serves clients across timezones (``Asia/Bangkok`` for JSC today,
``Asia/Almaty`` for the WhatsApp tenants in Kazakhstan later). Everything here is Bangkok
for now; a client-timezone parameter joins once a second timezone is actually wired up.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

BANGKOK_TZ = ZoneInfo("Asia/Bangkok")


def bangkok_now() -> dt.datetime:
    return dt.datetime.now(BANGKOK_TZ)


def bangkok_date() -> str:
    return bangkok_now().date().isoformat()


def days_until(expiry: str, *, now: dt.datetime | None = None) -> int:
    """Return calendar days from Bangkok today to an ISO date."""
    expiry_date = dt.date.fromisoformat(str(expiry).strip()[:10])
    current = now or bangkok_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=BANGKOK_TZ)
    else:
        current = current.astimezone(BANGKOK_TZ)
    return (expiry_date - current.date()).days
