#!/usr/bin/env python3
from __future__ import annotations

import getpass
import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
PLACEHOLDERS = ("replace_me", "your_", "LINE_BOT_USER_ID", "replace_with")


def read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    values: dict[str, str] = {}
    for line in lines:
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return lines, values


def usable(value: str | None) -> bool:
    return bool(value and not any(marker in value for marker in PLACEHOLDERS))


def secret(prompt: str, current: str | None = None) -> str:
    if usable(current):
        keep = input(f"{prompt} уже заполнен. Оставить текущее значение? [Y/n]: ").strip().lower()
        if keep in ("", "y", "yes", "д", "да"):
            return str(current)
    while True:
        value = getpass.getpass(f"{prompt}: ").strip()
        if value:
            return value
        print("Значение обязательно.")


def hidden_optional(prompt: str) -> str:
    return getpass.getpass(f"{prompt} (Enter — пропустить): ").strip()


def required(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip() or default
        if value:
            return value
        print("Значение обязательно.")


def fetch_bot_info(token: str) -> dict:
    request = urllib.request.Request(
        "https://api.line.me/v2/bot/info",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"LINE API отклонил token: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SystemExit(f"Не удалось обратиться к LINE API: {exc}") from exc


def replace_values(lines: list[str], updates: dict[str, str]) -> list[str]:
    result: list[str] = []
    written: set[str] = set()
    for line in lines:
        if line and not line.lstrip().startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in updates:
                result.append(f"{key}={updates[key]}")
                written.add(key)
                continue
        result.append(line)
    for key, value in updates.items():
        if key not in written:
            result.append(f"{key}={value}")
    return result


def main() -> None:
    lines, current = read_env(ENV_PATH)
    if not lines:
        raise SystemExit(f"Файл {ENV_PATH} не найден или пуст.")

    print("Вставляйте значения из Railway. Ввод секретов не отображается.")
    openai_key = secret("OPENAI_API_KEY", current.get("OPENAI_API_KEY"))
    line_token = secret("LINE_CHANNEL_ACCESS_TOKEN")
    line_secret = secret("LINE_CHANNEL_SECRET")
    owner_id = secret("MY_LINE_USER_ID")
    owner_2_id = hidden_optional("OWNER_2_LINE_USER_ID")
    sheet_id = secret("GOOGLE_SHEET_ID")
    sheet_gid = required("ID первой вкладки Google Sheets (gid)", "0")
    if not sheet_gid.isdigit():
        raise SystemExit("Google Sheets gid должен содержать только цифры.")
    google_raw = secret("GOOGLE_CREDENTIALS", current.get("GOOGLE_CREDENTIALS"))

    try:
        google_data = json.loads(google_raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"GOOGLE_CREDENTIALS содержит некорректный JSON: {exc}") from exc
    required_google = {"type", "project_id", "private_key", "client_email", "token_uri"}
    missing_google = sorted(required_google - set(google_data))
    if missing_google:
        raise SystemExit("В GOOGLE_CREDENTIALS отсутствуют поля: " + ", ".join(missing_google))

    bot_info = fetch_bot_info(line_token)
    destination = str(bot_info.get("userId") or "").strip()
    if not destination.startswith("U"):
        raise SystemExit("LINE API не вернул корректный Bot User ID.")

    name = required("Название клиента", "Just Specialty Coffee")
    business_type = required("Тип бизнеса", "cafe")
    custom_context = required(
        "Краткий контекст",
        "Кофейня в Таиланде; сотрудники общаются на русском, тайском и английском",
    )

    client = {
        "name": name,
        "business_type": business_type,
        "custom_context": custom_context,
        "channel_access_token": line_token,
        "channel_secret": line_secret,
        "owner_line_id": owner_id,
        "sheet_id": sheet_id,
        "sheet_gid": int(sheet_gid),
    }
    if owner_2_id:
        client["owner_line_id_2"] = owner_2_id

    updates = {
        "OPENAI_API_KEY": openai_key,
        "GOOGLE_CREDENTIALS": json.dumps(google_data, ensure_ascii=False, separators=(",", ":")),
        "CLIENTS_JSON": json.dumps({destination: client}, ensure_ascii=False, separators=(",", ":")),
    }
    new_lines = replace_values(lines, updates)

    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".env.", dir=ENV_PATH.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(new_lines).rstrip() + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, ENV_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

    print(f"Готово: {ENV_PATH}")
    print(f"LINE bot: {bot_info.get('displayName', 'unknown')} ({bot_info.get('basicId', 'unknown')})")
    print("Секреты и CLIENTS_JSON сохранены с правами 600.")


if __name__ == "__main__":
    main()
