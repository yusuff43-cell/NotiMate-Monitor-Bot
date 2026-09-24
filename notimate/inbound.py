"""Channel-agnostic inbound message contract (Этап 2/3 of docs/21).

Deliberately scoped down from the full docs/21 Этап 2 plan: instead of migrating the
proven, live `line_events` table (event_store.py) to a generalized shape on faith, LINE
keeps its own untouched pipeline and this contract/table start life serving only the new
WhatsApp channel, which has no live traffic yet. Once WhatsApp has run in production for a
while, folding LINE into the same `inbound_events` table becomes a much lower-risk, better
-informed change than guessing the right generalization up front.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class InboundMessage:
    tenant_id: str
    channel: str
    external_event_id: str
    chat_id: str
    sender_id: str
    sender_role: str  # 'owner' / 'staff' / 'accountant'
    text: str
    media: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    received_at: dt.datetime | None = None
    raw_ref: str | None = None


def build_whatsapp_inbound_message(channel_row: Mapping[str, Any], message: Mapping[str, Any]) -> InboundMessage:
    """Normalize one WhatsApp Cloud API message + its tenant_store row into an InboundMessage."""
    tenant = channel_row['tenant']
    channel = channel_row['channel']
    sender_id = str(message.get('from') or '')
    owner_ids = {str(o) for o in (channel.get('owner_ids') or [])}
    text = ''
    if message.get('type') == 'text':
        text = str(message.get('text', {}).get('body') or '')
    elif message.get('type') == 'interactive':
        interactive = message.get('interactive', {})
        text = str(
            interactive.get('button_reply', {}).get('id')
            or interactive.get('list_reply', {}).get('id')
            or ''
        )
    media: list[dict[str, Any]] = []
    if message.get('type') == 'image':
        image = message.get('image', {})
        if image.get('id'):
            media.append({'kind': 'image', 'id': str(image['id']), 'mime_type': str(image.get('mime_type') or 'image/jpeg')})
        text = str(image.get('caption') or '')
    received_at = None
    timestamp = message.get('timestamp')
    if timestamp:
        try:
            received_at = dt.datetime.fromtimestamp(int(timestamp), tz=dt.timezone.utc)
        except (TypeError, ValueError):
            received_at = None
    return InboundMessage(
        tenant_id=tenant['id'],
        channel='whatsapp',
        external_event_id=str(message.get('id') or ''),
        chat_id=sender_id,
        sender_id=sender_id,
        sender_role='owner' if sender_id in owner_ids else 'staff',
        text=text,
        media=tuple(media),
        received_at=received_at,
    )
