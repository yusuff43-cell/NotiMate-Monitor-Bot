"""WhatsApp Cloud API adapter (Этап 3 of docs/21) — signature, webhook parsing, sending.

Patterns carried over from `Клиенты/Ержан — 3 бизнеса/notimate-bot/whatsapp_webhook.py`
(``verify_signature``, ``send_message``, interactive buttons), adapted for a multi-tenant
Core: credentials are passed in per call (``access_token``/``phone_number_id`` come from a
tenant_channels row) instead of read from process-wide env vars, since one process now
serves many WhatsApp numbers instead of Erzhan's single pilot bot.

Not wired into any Flask route yet — no WhatsApp number is provisioned (Meta payment method
and business verification are still pending, [[20 - Сайты, бренды и Facebook]]). These are
pure, fully unit-tested functions ready for the webhook route once that exists.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import urllib.error
import urllib.request

GRAPH_VERSION = 'v23.0'
GRAPH_BASE_URL = 'https://graph.facebook.com'

# Meta closes free-form replies 24h after the customer's last message; after that only
# pre-approved template messages may be sent (docs/21 Этап 3).
FREE_FORM_WINDOW = dt.timedelta(hours=24)


def verify_signature(app_secret: str, body: bytes, signature_header: str) -> bool:
    """Check the raw webhook body against Meta's `X-Hub-Signature-256` header."""
    if not signature_header.startswith('sha256='):
        return False
    expected = 'sha256=' + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header, expected)


def verify_webhook_challenge(verify_token: str, mode: str, token: str, challenge: str) -> str | None:
    """Return the challenge to echo back for Meta's GET /webhook handshake, or None to reject."""
    if mode == 'subscribe' and hmac.compare_digest(token or '', verify_token):
        return challenge
    return None


def extract_messages(payload: dict) -> list[dict]:
    """Flatten a Cloud API webhook body into one dict per inbound message.

    Each entry carries ``phone_number_id`` (the tenant-routing key, analogous to LINE's
    ``destination``) alongside the raw WhatsApp ``message`` object. Delivery-status
    callbacks (``statuses``, e.g. sent/delivered/read/failed) are not messages and are
    skipped here — the caller never sees them.
    """
    messages = []
    for entry in payload.get('entry', []):
        for change in entry.get('changes', []):
            value = change.get('value', {})
            phone_number_id = value.get('metadata', {}).get('phone_number_id', '')
            for message in value.get('messages', []):
                messages.append({'phone_number_id': phone_number_id, 'message': message})
    return messages


def is_within_free_form_window(last_inbound_at: dt.datetime | None, now: dt.datetime) -> bool:
    """True while a free-form reply is still allowed after the customer's last message."""
    if last_inbound_at is None:
        return False
    if last_inbound_at.tzinfo is None:
        last_inbound_at = last_inbound_at.replace(tzinfo=dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    return now - last_inbound_at < FREE_FORM_WINDOW


def _post(access_token: str, phone_number_id: str, payload: dict) -> dict:
    body = json.dumps({'messaging_product': 'whatsapp', **payload}).encode('utf-8')
    request = urllib.request.Request(
        f'{GRAPH_BASE_URL}/{GRAPH_VERSION}/{phone_number_id}/messages',
        data=body,
        method='POST',
        headers={
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'WhatsApp send failed ({exc.code}): {detail}') from exc


def send_text(access_token: str, phone_number_id: str, recipient: str, body: str) -> dict:
    return _post(access_token, phone_number_id, {
        'to': recipient,
        'type': 'text',
        'text': {'body': body},
    })


def send_interactive_buttons(access_token: str, phone_number_id: str, recipient: str, body: str, buttons: list[tuple[str, str]]) -> dict:
    """Send up to 3 reply buttons. ``buttons`` is a list of ``(id, title)`` pairs."""
    if not 1 <= len(buttons) <= 3:
        raise ValueError('WhatsApp interactive button messages need 1-3 buttons')
    return _post(access_token, phone_number_id, {
        'to': recipient,
        'type': 'interactive',
        'interactive': {
            'type': 'button',
            'body': {'text': body},
            'action': {'buttons': [
                {'type': 'reply', 'reply': {'id': button_id, 'title': title}}
                for button_id, title in buttons
            ]},
        },
    })
