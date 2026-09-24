"""Tenant configuration contract shared by every channel adapter."""

from __future__ import annotations

from collections.abc import Mapping

SUPPORTED_INPUT_LANGUAGES = ("ru", "th", "en")
OWNER_OUTPUT_LANGUAGE = "ru"
REQUIRED_CLIENT_FIELDS = (
    "channel_access_token",
    "channel_secret",
    "owner_line_id",
    "sheet_id",
)


def validate_clients(clients: object) -> dict[str, Mapping[str, object]]:
    if not isinstance(clients, dict) or not clients:
        raise ValueError("CLIENTS_JSON must be a non-empty object keyed by LINE destination")

    errors: list[str] = []
    for destination, config in clients.items():
        if not isinstance(destination, str) or not destination.strip():
            errors.append("client destination must be a non-empty string")
            continue
        if not isinstance(config, Mapping):
            errors.append(f"{destination}: configuration must be an object")
            continue
        missing = [
            field
            for field in REQUIRED_CLIENT_FIELDS
            if not isinstance(config.get(field), str) or not str(config.get(field)).strip()
        ]
        if missing:
            errors.append(f"{destination}: missing {', '.join(missing)}")
        allowed = config.get("allowed_group_ids")
        if allowed is not None and (
            not isinstance(allowed, list)
            or any(not isinstance(item, str) or not item.strip() for item in allowed)
        ):
            errors.append(f"{destination}: allowed_group_ids must be a list of non-empty strings")

    if errors:
        raise ValueError("Invalid client configuration: " + "; ".join(errors))
    return clients


def is_group_allowed(client_cfg: Mapping[str, object], source: Mapping[str, object]) -> bool:
    """Decide whether a group/room event may be processed for this client.

    ``allowed_group_ids`` absent -> legacy behaviour, every group of the bot is accepted.
    ``allowed_group_ids`` present (even empty) -> only listed groupId/roomId are accepted.
    """
    allowed = client_cfg.get("allowed_group_ids")
    if allowed is None:
        return True
    group_id = source.get("groupId") or source.get("roomId")
    return isinstance(group_id, str) and group_id in allowed


def client_prompt_context(client_cfg: Mapping[str, object]) -> str:
    name = str(client_cfg.get("name") or "business")
    business_type = str(client_cfg.get("business_type") or "small business")
    custom_context = str(client_cfg.get("custom_context") or "").strip()
    context = f"Название клиента: {name}. Тип бизнеса: {business_type}."
    if custom_context:
        context += f" Контекст клиента: {custom_context}"
    return context


def find_client(clients: Mapping[str, Mapping[str, object]], destination: str):
    return clients.get(destination)


def channel_row_to_client_cfg(row: Mapping[str, object], secret: Mapping[str, object] | None) -> dict[str, object] | None:
    """Build a legacy-shaped ``client_cfg`` from a ``tenant_store.find_channel`` row.

    Every handler still reads ``client_cfg['channel_access_token']`` etc. exactly as when
    all configuration came from ``CLIENTS_JSON``, so this is the one place that adapts the
    tenants/tenant_channels row (docs/21 Этап 2) to that unchanged shape. Returns ``None``
    when ``secret`` doesn't actually hold LINE credentials — the caller falls back to
    ``CLIENTS_JSON`` rather than serve a half-populated config to production.
    """
    if not secret:
        return None
    tenant = row['tenant']
    channel = row['channel']
    token = secret.get('channel_access_token') if isinstance(secret, Mapping) else None
    channel_secret = secret.get('channel_secret') if isinstance(secret, Mapping) else None
    owner_ids = [str(o) for o in (channel.get('owner_ids') or []) if str(o or '').strip()]
    if not (token and channel_secret and owner_ids and tenant.get('sheet_id')):
        return None
    cfg: dict[str, object] = {
        'channel_access_token': token,
        'channel_secret': channel_secret,
        'owner_line_id': owner_ids[0],
        'sheet_id': tenant['sheet_id'],
    }
    if len(owner_ids) > 1:
        cfg['owner_line_id_2'] = owner_ids[1]
    if tenant.get('name'):
        cfg['name'] = tenant['name']
    if tenant.get('business_type'):
        cfg['business_type'] = tenant['business_type']
    if tenant.get('custom_context'):
        cfg['custom_context'] = tenant['custom_context']
    if tenant.get('country'):
        cfg['country'] = tenant['country']
    if tenant.get('modules'):
        cfg['modules'] = tenant['modules']
    if channel.get('allowed_chats') is not None:
        cfg['allowed_group_ids'] = list(channel['allowed_chats'])
    return cfg


def whatsapp_channel_config(row: Mapping[str, object], secret: Mapping[str, object] | None) -> dict[str, object] | None:
    """Build a WhatsApp-shaped config from a tenant_store row, parallel to
    ``channel_row_to_client_cfg`` but with WhatsApp's own field names — the two channels'
    credentials genuinely don't share a shape (Bearer token + phone_number_id here, LINE
    channel token + secret there), so forcing one function to cover both would just hide
    that difference behind optional fields. Returns ``None`` when required fields are
    missing, so the caller can refuse to process rather than guess.

    Only ``access_token`` (per tenant/number) comes from ``secret`` here. The webhook
    signature's app secret is *not* per-tenant — one Meta App, and therefore one app
    secret, can front many tenants' WhatsApp numbers, so it is verified once at the route
    level (``app.WHATSAPP_APP_SECRET``) before any tenant is even resolved, not per-channel.
    """
    if not secret:
        return None
    tenant = row['tenant']
    channel = row['channel']
    access_token = secret.get('access_token') if isinstance(secret, Mapping) else None
    owner_ids = [str(o) for o in (channel.get('owner_ids') or []) if str(o or '').strip()]
    if not (access_token and owner_ids):
        return None
    return {
        'access_token': access_token,
        'phone_number_id': channel['external_id'],
        'owner_ids': owner_ids,
        'name': tenant.get('name'),
        'timezone': tenant.get('timezone'),
        'sheet_id': tenant.get('sheet_id'),
        'business_type': tenant.get('business_type'),
        'custom_context': tenant.get('custom_context'),
    }


def cfg_currency(client_cfg: Mapping[str, object]) -> str:
    """Currency label for messages, ledger rows and Sheets headers; THB unless the tenant says otherwise."""
    return str(client_cfg.get("currency") or "THB")
