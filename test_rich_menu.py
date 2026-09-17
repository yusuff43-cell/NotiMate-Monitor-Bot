import importlib.util
import struct
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent / "deploy" / "configure_owner_rich_menu.py"
SPEC = importlib.util.spec_from_file_location("configure_owner_rich_menu", SCRIPT)
rich_menu = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(rich_menu)


class RichMenuTests(unittest.TestCase):
    def test_safe_url_redacts_owner_id(self):
        url = "https://api.line.me/v2/bot/user/U-secret-owner/richmenu/richmenu-123"
        self.assertEqual(
            rich_menu.safe_url(url),
            "https://api.line.me/v2/bot/user/<redacted>/richmenu/richmenu-123",
        )

    def test_payload_covers_canvas_and_uses_existing_owner_commands(self):
        payload = rich_menu.menu_payload("sheet-id", b"image", 1180783168)
        self.assertEqual(payload["size"], {"width": 2500, "height": 843})
        self.assertTrue(payload["selected"])
        self.assertEqual([area["bounds"]["x"] for area in payload["areas"]], [0, 625, 1250, 1875])
        self.assertTrue(all(area["bounds"]["width"] == 625 for area in payload["areas"]))
        self.assertEqual(
            [area["action"].get("text") for area in payload["areas"][:3]],
            ["подробный отчёт", "деньги", "напоминания"],
        )
        self.assertEqual(
            payload["areas"][3]["action"]["uri"],
            "https://docs.google.com/spreadsheets/d/sheet-id/edit#gid=1180783168",
        )

    def test_sheet_url_rejects_invalid_gid(self):
        with self.assertRaisesRegex(ValueError, "only digits"):
            rich_menu.sheet_url("sheet-id", "0&unexpected=true")

    def test_payload_name_changes_with_image_or_sheet(self):
        first = rich_menu.menu_payload("sheet-a", b"image-a")["name"]
        self.assertNotEqual(first, rich_menu.menu_payload("sheet-a", b"image-b")["name"])
        self.assertNotEqual(first, rich_menu.menu_payload("sheet-b", b"image-a")["name"])
        self.assertNotEqual(first, rich_menu.menu_payload("sheet-a", b"image-a", 123)["name"])

    def test_read_env_preserves_json_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text('# comment\nCLIENTS_JSON={"Ubot":{"name":"Кафе"}}\n', encoding="utf-8")
            self.assertEqual(rich_menu.read_env(path)["CLIENTS_JSON"], '{"Ubot":{"name":"Кафе"}}')

    def test_png_validation_rejects_wrong_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "menu.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 1200, 800))
            with self.assertRaisesRegex(ValueError, "2500x843"):
                rich_menu.validate_png(path)


if __name__ == "__main__":
    unittest.main()
