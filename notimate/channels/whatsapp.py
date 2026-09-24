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


def configure_conversational_automation(
    access_token: str, phone_number_id: str,
    commands: list[tuple[str, str]] | None = None,
    prompts: list[str] | None = None,
    enable_welcome_message: bool | None = None,
) -> dict:
    """Set the number's «/» command list and ice breaker prompts.

    WhatsApp has no persistent docked button bar like LINE's Rich Menu — this is the
    closest real equivalent: ``commands`` (up to 30, shown when the user types "/" in the
    message box) and ``prompts`` (up to 4 ice breakers, shown only before the very first
    message in a new chat). ``commands`` is a list of ``(command_name, command_description)``
    pairs — Meta limits ``command_name`` to 32 characters and ``command_description`` to
    256. One-time per-number config, not sent with every message; call once (e.g. from a
    deploy script), not from the message-handling path.

    https://developers.facebook.com/docs/whatsapp/cloud-api/phone-numbers/conversational-components/
    """
    payload: dict = {}
    if commands is not None:
        if len(commands) > 30:
            raise ValueError('WhatsApp allows at most 30 commands')
        payload['commands'] = [
            {'command_name': name, 'command_description': description} for name, description in commands
        ]
    if prompts is not None:
        if len(prompts) > 4:
            raise ValueError('WhatsApp allows at most 4 ice breaker prompts')
        payload['prompts'] = prompts
    if enable_welcome_message is not None:
        payload['enable_welcome_message'] = enable_welcome_message

    body = json.dumps(payload).encode('utf-8')
    request = urllib.request.Request(
        f'{GRAPH_BASE_URL}/{GRAPH_VERSION}/{phone_number_id}/conversational_automation',
        data=body,
        method='POST',
        headers={'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'WhatsApp conversational_automation config failed ({exc.code}): {detail}') from exc


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


def _get(access_token: str, url: str) -> bytes:
    request = urllib.request.Request(url, headers={'Authorization': f'Bearer {access_token}'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'WhatsApp media request failed ({exc.code}): {detail}') from exc


def download_media(access_token: str, media_id: str, max_bytes: int = 10 * 1024 * 1024) -> tuple[bytes, str]:
    """Fetch an inbound media file (photo) by its Cloud API media id.

    Two calls, as Meta documents: GET /{media_id} returns a short-lived URL plus the mime
    type, then that URL is fetched with the same Bearer token. The size is checked before
    the file is downloaded so an oversized upload can't exhaust the 1.9 GB host.
    """
    meta = json.loads(_get(access_token, f'{GRAPH_BASE_URL}/{GRAPH_VERSION}/{media_id}').decode('utf-8') or '{}')
    url = meta.get('url')
    if not url:
        raise RuntimeError('WhatsApp media metadata has no url')
    declared = int(meta.get('file_size') or 0)
    if declared and declared > max_bytes:
        raise RuntimeError(f'WhatsApp media too large ({declared} bytes)')
    data = _get(access_token, url)
    if len(data) > max_bytes:
        raise RuntimeError(f'WhatsApp media too large ({len(data)} bytes)')
    return data, str(meta.get('mime_type') or 'image/jpeg')


def _multipart(fields: dict[str, str], file_field: str, filename: str, mime: str, data: bytes) -> tuple[bytes, str]:
    boundary = 'notimate' + hashlib.sha256(data[:4096] + filename.encode()).hexdigest()[:24]
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode('utf-8')
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f'Content-Type: {mime}\r\n\r\n'.encode('utf-8') + data + b'\r\n'
    )
    parts.append(f'--{boundary}--\r\n'.encode('utf-8'))
    return b''.join(parts), f'multipart/form-data; boundary={boundary}'


def upload_media(access_token: str, phone_number_id: str, filename: str, mime: str, data: bytes) -> str:
    """Upload a file to WhatsApp and return its media id (used to send documents)."""
    body, content_type = _multipart({'messaging_product': 'whatsapp', 'type': mime}, 'file', filename, mime, data)
    request = urllib.request.Request(
        f'{GRAPH_BASE_URL}/{GRAPH_VERSION}/{phone_number_id}/media',
        data=body,
        method='POST',
        headers={'Authorization': f'Bearer {access_token}', 'Content-Type': content_type},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            media_id = json.loads(response.read().decode('utf-8') or '{}').get('id')
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'WhatsApp media upload failed ({exc.code}): {detail}') from exc
    if not media_id:
        raise RuntimeError('WhatsApp media upload returned no id')
    return str(media_id)


def send_document(
    access_token: str, phone_number_id: str, recipient: str,
    filename: str, mime: str, data: bytes, caption: str = '',
) -> dict:
    media_id = upload_media(access_token, phone_number_id, filename, mime, data)
    document: dict = {'id': media_id, 'filename': filename}
    if caption:
        document['caption'] = caption
    return _post(access_token, phone_number_id, {'to': recipient, 'type': 'document', 'document': document})


# ── proactive messages outside the 24-hour window ───────────────────────────────────────

# Meta refuses free-form text 24 h after the customer's last message (error 131047 "Re-engagement
# message"); only an approved template may open the conversation again.
WINDOW_ERROR_MARKERS = ('131047', 're-engagement', 'Re-engagement')


def _template_text(body: str) -> str:
    """Template variables may not contain newlines/tabs or long space runs (Meta rule)."""
    flat = ' | '.join(part.strip() for part in body.replace('\t', ' ').splitlines() if part.strip())
    return ' '.join(flat.split(' '))[:1000] or '—'


def send_template(access_token: str, phone_number_id: str, recipient: str, name: str, language: str, body: str) -> dict:
    """Send an approved utility template whose body has ONE variable ({{1}}) carrying the text."""
    return _post(access_token, phone_number_id, {
        'to': recipient,
        'type': 'template',
        'template': {
            'name': name,
            'language': {'code': language},
            'components': [{'type': 'body', 'parameters': [{'type': 'text', 'text': _template_text(body)}]}],
        },
    })


def fallback_template(environ: dict | None = None) -> tuple[str, str] | None:
    """``WHATSAPP_FALLBACK_TEMPLATE=name:lang`` (e.g. ``notimate_update:ru``) or None when unset."""
    import os
    raw = ((environ or os.environ).get('WHATSAPP_FALLBACK_TEMPLATE') or '').strip()
    if not raw:
        return None
    name, _, language = raw.partition(':')
    return (name, language or 'ru') if name else None


def send_proactive(access_token: str, phone_number_id: str, recipient: str, body: str) -> dict:
    """For bot-initiated messages (summaries, digests, package, alerts): free text first, and if
    WhatsApp says the 24-hour window is closed, the configured template instead. Without a
    configured template the original error is raised so callers still log the failure."""
    try:
        return send_text(access_token, phone_number_id, recipient, body)
    except RuntimeError as exc:
        template = fallback_template()
        if template is None or not any(marker in str(exc) for marker in WINDOW_ERROR_MARKERS):
            raise
        return send_template(access_token, phone_number_id, recipient, template[0], template[1], body)
