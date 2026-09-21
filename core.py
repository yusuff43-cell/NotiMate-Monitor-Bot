from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from zoneinfo import ZoneInfo


BANGKOK_TZ = ZoneInfo("Asia/Bangkok")
SUPPORTED_INPUT_LANGUAGES = ("ru", "th", "en")
OWNER_OUTPUT_LANGUAGE = "ru"
REQUIRED_CLIENT_FIELDS = (
    "channel_access_token",
    "channel_secret",
    "owner_line_id",
    "sheet_id",
)


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
