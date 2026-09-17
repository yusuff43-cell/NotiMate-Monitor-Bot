import datetime as dt
import unittest

from core import (
    BANGKOK_TZ,
    OWNER_OUTPUT_LANGUAGE,
    SUPPORTED_INPUT_LANGUAGES,
    client_prompt_context,
    days_until,
    validate_clients,
)


VALID_CLIENT = {
    "channel_access_token": "token",
    "channel_secret": "secret",
    "owner_line_id": "owner",
    "sheet_id": "sheet",
}


class CoreTests(unittest.TestCase):
    def test_days_until_uses_bangkok_calendar_date(self):
        now = dt.datetime(2026, 9, 17, 23, 30, tzinfo=BANGKOK_TZ)
        self.assertEqual(days_until("2026-09-18", now=now), 1)

    def test_days_until_accepts_timestamp_prefix(self):
        now = dt.datetime(2026, 9, 17, 8, 0, tzinfo=BANGKOK_TZ)
        self.assertEqual(days_until("2026-09-24T00:00:00", now=now), 7)

    def test_validate_clients_accepts_complete_destination_config(self):
        clients = {"Udestination": dict(VALID_CLIENT)}
        self.assertIs(validate_clients(clients), clients)

    def test_validate_clients_lists_missing_fields(self):
        with self.assertRaisesRegex(ValueError, "owner_line_id, sheet_id"):
            validate_clients({"Udestination": {"channel_access_token": "x", "channel_secret": "y"}})

    def test_client_prompt_context_uses_real_client_fields(self):
        context = client_prompt_context(
            {"name": "Cafe A", "business_type": "cafe", "custom_context": "Thai bakery"}
        )
        self.assertIn("Cafe A", context)
        self.assertIn("Thai bakery", context)
        self.assertIn("Название клиента", context)

    def test_language_contract_is_russian_owner_with_multilingual_input(self):
        self.assertEqual(OWNER_OUTPUT_LANGUAGE, "ru")
        self.assertEqual(SUPPORTED_INPUT_LANGUAGES, ("ru", "th", "en"))


if __name__ == "__main__":
    unittest.main()
