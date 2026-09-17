#!/usr/bin/env python3
"""Create and link a permanent LINE Rich Menu to configured owners only."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


API_URL = "https://api.line.me/v2/bot"
DATA_API_URL = "https://api-data.line.me/v2/bot"
MENU_NAME_PREFIX = "NotiMate owner menu v1"


def safe_url(url: str) -> str:
    return re.sub(r"/user/[^/]+/", "/user/<redacted>/", url)


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def validate_png(path: Path) -> bytes:
    data = path.read_bytes()
    if len(data) > 1024 * 1024:
        raise ValueError(f"Rich Menu image is larger than 1 MB: {len(data)} bytes")
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Rich Menu image must be a PNG file")
    width, height = struct.unpack(">II", data[16:24])
    if (width, height) != (2500, 843):
        raise ValueError(f"Expected a 2500x843 image, got {width}x{height}")
    return data


def menu_payload(sheet_id: str, image: bytes) -> dict[str, Any]:
    areas = [
        {
            "bounds": {"x": 0, "y": 0, "width": 625, "height": 843},
            "action": {"type": "message", "label": "Подробный отчёт", "text": "подробный отчёт"},
        },
        {
            "bounds": {"x": 625, "y": 0, "width": 625, "height": 843},
            "action": {"type": "message", "label": "Деньги", "text": "деньги"},
        },
        {
            "bounds": {"x": 1250, "y": 0, "width": 625, "height": 843},
            "action": {"type": "message", "label": "Напоминания", "text": "напоминания"},
        },
        {
            "bounds": {"x": 1875, "y": 0, "width": 625, "height": 843},
            "action": {
                "type": "uri",
                "label": "Открыть таблицу",
                "uri": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit",
            },
        },
    ]
    stable = {
        "size": {"width": 2500, "height": 843},
        "selected": True,
        "chatBarText": "Отчёты",
        "areas": areas,
    }
    digest_source = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(image + digest_source).hexdigest()[:12]
    return {**stable, "name": f"{MENU_NAME_PREFIX} {digest}"}


class LineApi:
    def __init__(self, token: str):
        self.token = token

    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        binary_body: bytes | None = None,
        content_type: str | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {self.token}", "User-Agent": "NotiMate-rich-menu/1.0"}
        body = binary_body
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                content = response.read()
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"LINE API {method} {safe_url(url)} failed with HTTP {exc.code}: {details}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"LINE API {method} {safe_url(url)} is unavailable: {exc}") from exc
        return json.loads(content) if content else {}

    def validate(self, payload: dict[str, Any]) -> None:
        self.request("POST", f"{API_URL}/richmenu/validate", json_body=payload)

    def list_menus(self) -> list[dict[str, Any]]:
        result = self.request("GET", f"{API_URL}/richmenu/list")
        return list(result.get("richmenus", []))

    def create_menu(self, payload: dict[str, Any]) -> str:
        result = self.request("POST", f"{API_URL}/richmenu", json_body=payload)
        menu_id = str(result.get("richMenuId", ""))
        if not menu_id:
            raise RuntimeError("LINE API created a menu without returning richMenuId")
        return menu_id

    def upload_image(self, menu_id: str, image: bytes) -> None:
        self.request(
            "POST",
            f"{DATA_API_URL}/richmenu/{menu_id}/content",
            binary_body=image,
            content_type="image/png",
        )

    def link_owner(self, owner_id: str, menu_id: str) -> None:
        self.request("POST", f"{API_URL}/user/{owner_id}/richmenu/{menu_id}")

    def linked_menu(self, owner_id: str) -> str:
        result = self.request("GET", f"{API_URL}/user/{owner_id}/richmenu")
        return str(result.get("richMenuId", ""))

    def delete_menu(self, menu_id: str) -> None:
        self.request("DELETE", f"{API_URL}/richmenu/{menu_id}")


def configure_client(client: dict[str, Any], image: bytes, dry_run: bool = False) -> str:
    required = ("channel_access_token", "owner_line_id", "sheet_id")
    missing = [key for key in required if not str(client.get(key, "")).strip()]
    if missing:
        raise ValueError("Client configuration is missing: " + ", ".join(missing))

    payload = menu_payload(str(client["sheet_id"]), image)
    owners = [str(client["owner_line_id"])]
    if client.get("owner_line_id_2"):
        owners.append(str(client["owner_line_id_2"]))
    if dry_run:
        return f"DRY RUN: {payload['name']}; owners={len(owners)}"

    api = LineApi(str(client["channel_access_token"]))
    api.validate(payload)
    existing = next((menu for menu in api.list_menus() if menu.get("name") == payload["name"]), None)
    created = existing is None
    menu_id = api.create_menu(payload) if created else str(existing["richMenuId"])
    try:
        if created:
            api.upload_image(menu_id, image)
        for owner_id in owners:
            api.link_owner(owner_id, menu_id)
            linked = api.linked_menu(owner_id)
            if linked != menu_id:
                raise RuntimeError("LINE accepted the link request, but owner Rich Menu verification failed")
    except Exception:
        if created:
            try:
                api.delete_menu(menu_id)
            except Exception as cleanup_error:
                print(f"Warning: couldn't remove incomplete Rich Menu: {cleanup_error}", file=sys.stderr)
        raise
    return f"Linked {menu_id} to {len(owners)} owner(s)"


def parse_args() -> argparse.Namespace:
    project_dir = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=project_dir / ".env", help="Path to the deployment .env")
    parser.add_argument(
        "--image",
        type=Path,
        default=project_dir / "assets" / "owner-rich-menu.png",
        help="2500x843 PNG image (maximum 1 MB)",
    )
    parser.add_argument("--destination", help="Configure only this CLIENTS_JSON destination")
    parser.add_argument("--dry-run", action="store_true", help="Validate local configuration without calling LINE")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    env = read_env(args.env)
    try:
        clients = json.loads(env["CLIENTS_JSON"])
    except KeyError as exc:
        raise SystemExit(f"CLIENTS_JSON is missing in {args.env}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"CLIENTS_JSON is invalid in {args.env}: {exc}") from exc
    if args.destination:
        if args.destination not in clients:
            raise SystemExit("Requested destination is not present in CLIENTS_JSON")
        clients = {args.destination: clients[args.destination]}
    image = validate_png(args.image)
    for destination, client in clients.items():
        label = client.get("name") or f"LINE destination …{destination[-6:]}"
        print(f"{label}: {configure_client(client, image, dry_run=args.dry_run)}")


if __name__ == "__main__":
    main()
