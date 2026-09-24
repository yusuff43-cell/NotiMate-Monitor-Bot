"""Signed, short-lived tokens for the owner dashboard and month-package downloads.

Pattern from СтройКонтроль ``dashboard_links.py`` (HMAC over the parameters, expiry checked
on verify), generalised for tenants and channels: a token binds ``tenant`` + ``subject`` (the
owner's channel id) + ``scope`` (+ optional period) + expiry under one HMAC-SHA256, so no
field can be changed without invalidating it. Tokens never contain data — only the identity
and what they may open; the API resolves everything else server-side from the tenant.

Scopes: ``link`` (one-time-ish login link, 5 min → exchanged for a session cookie),
``session`` (HttpOnly cookie, 12 h), ``package`` (direct package download, 15 min).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

LINK_TTL = 300
SESSION_TTL = 12 * 3600
PACKAGE_TTL = 900
SCOPES = ('link', 'session', 'package')


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def _sign(secret: str, body: str) -> str:
    return hmac.new(secret.encode(), body.encode('ascii'), hashlib.sha256).hexdigest()


def issue_token(secret: str, tenant_id: str, subject: str, scope: str, ttl: int, period: str | None = None, now: int | None = None) -> str:
    if scope not in SCOPES:
        raise ValueError(f'unknown scope {scope!r}')
    if not secret:
        raise ValueError('signing secret is required')
    claims: dict[str, Any] = {'t': tenant_id, 's': subject, 'sc': scope, 'e': (int(time.time()) if now is None else now) + ttl}
    if period:
        claims['p'] = period
    body = _b64(json.dumps(claims, separators=(',', ':'), sort_keys=True).encode())
    return f'{body}.{_sign(secret, body)}'


def verify_token(secret: str, token: str, scope: str, now: int | None = None) -> dict[str, Any] | None:
    """Return the claims for a genuine, unexpired token of the requested scope, else None."""
    if not secret or not token or token.count('.') != 1:
        return None
    body, signature = token.split('.')
    if not hmac.compare_digest(_sign(secret, body), signature):
        return None
    try:
        claims = json.loads(_unb64(body))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(claims, dict) or claims.get('sc') != scope:
        return None
    if int(claims.get('e', 0)) < (int(time.time()) if now is None else now):
        return None
    if not claims.get('t') or not claims.get('s'):
        return None
    return claims


def build_login_url(api_base: str, secret: str, tenant_id: str, subject: str) -> str:
    token = issue_token(secret, tenant_id, subject, 'link', LINK_TTL)
    return f"{api_base.rstrip('/')}/auth/link?t={token}"


def build_package_url(api_base: str, secret: str, tenant_id: str, subject: str, period: str) -> str:
    token = issue_token(secret, tenant_id, subject, 'package', PACKAGE_TTL, period)
    return f"{api_base.rstrip('/')}/d/package?t={token}"
