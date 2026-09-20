"""Fixture-driven regression tests for everything downstream of the model call.

The OpenAI answer is replaced by the recorded ``model_output`` of each scenario in
``fixtures/real_patterns.json``.  The tests then drive the real ``process_line_event``
path (routing, JSON extraction, Sheets projection, owner notification) against an
in-memory spreadsheet.  Whether the *model* returns these answers is checked by the
opt-in ``deploy/eval_live_fixtures.py``, not here, so this suite is deterministic and
needs no network or API key.
"""

import json
import os
import re
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('OPENAI_API_KEY', 'test-key')
os.environ.setdefault('GOOGLE_CREDENTIALS', '{}')
os.environ.setdefault('DISABLE_SCHEDULER', '1')
os.environ.setdefault('CLIENTS_JSON', json.dumps({
    'Ubot': {
        'channel_access_token': 'test-token',
        'channel_secret': 'test-secret',
        'owner_line_id': 'Uowner',
        'sheet_id': 'test-sheet',
    }
}))

import importlib  # noqa: E402

from fixture_support import classify_model_output, load_scenarios  # noqa: E402
from test_sheets import FakeGC, FakeSpreadsheet, message_event  # noqa: E402

app_module = importlib.import_module('app')

HANDLED_TYPES = {
    # text
    'stock', 'purchase', 'single_stock', 'text_expense', 'problem', 'ignore',
    # image
    'shift', 'invoice', 'expense', 'salary', 'reminder', 'notice', 'bank_history', 'not_finance',
}
LOUD_FAILURES = {'malformed', 'missing_type'}


def data_rows(worksheet):
    return max(len(worksheet.rows) - 1, 0)


def column(worksheet, header):
    index = worksheet.rows[0].index(header)
    return [row[index] for row in worksheet.rows[1:]]


def iter_strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from iter_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_strings(value)


class FixtureRegressionTests(unittest.TestCase):
    def setUp(self):
        self.scenarios = load_scenarios()
        self.original_gc = app_module.gc
        self.original_sheets_enabled = app_module.SHEETS_ENABLED
        self.addCleanup(setattr, app_module, 'gc', self.original_gc)
        self.addCleanup(setattr, app_module, 'SHEETS_ENABLED', self.original_sheets_enabled)
        app_module.SHEETS_ENABLED = True

        blob = Mock()
        blob.get_message_content.return_value = b'not-a-real-image'
        self.notify = patch.object(app_module, 'notify_owner').start()
        patch.object(app_module, 'refresh_overview').start()
        patch.object(app_module, 'get_line_blob_api', return_value=blob).start()
        self.addCleanup(patch.stopall)

    def fresh_spreadsheet(self):
        self.spreadsheet = FakeSpreadsheet()
        app_module.gc = FakeGC(self.spreadsheet)
        self.notify.reset_mock()

    def deliver(self, scenario, event_id):
        channel = scenario['channel']
        target = 'analyze_text' if channel == 'text' else 'analyze_image'
        event = message_event(event_id, channel, scenario.get('message', ''))
        with patch.object(app_module, target, return_value=scenario['model_output']):
            app_module.process_line_event('Ubot', event)

    def row_counts(self):
        return {name: data_rows(sheet) for name, sheet in self.spreadsheet.sheets.items()}

    def notifications(self):
        return [call.args[1] for call in self.notify.call_args_list]

    # -- fixture hygiene ---------------------------------------------------------------

    def test_declared_type_matches_recorded_model_output(self):
        for scenario in self.scenarios:
            with self.subTest(scenario=scenario['id']):
                self.assertEqual(
                    scenario['expected_type'],
                    classify_model_output(scenario['channel'], scenario['model_output']),
                )

    def test_fixture_set_covers_languages_channels_and_every_handled_type(self):
        self.assertEqual({'ru', 'th', 'en'}, {s['language'] for s in self.scenarios})
        self.assertEqual({'text', 'image'}, {s['channel'] for s in self.scenarios})
        covered = {s['expected_type'] for s in self.scenarios}
        self.assertEqual(set(), HANDLED_TYPES - covered)
        self.assertEqual(LOUD_FAILURES, covered & LOUD_FAILURES)
        ids = [s['id'] for s in self.scenarios]
        self.assertEqual(len(ids), len(set(ids)), 'scenario ids must be unique')

    def test_fixtures_contain_no_personal_identifiers(self):
        raw = json.loads(json.dumps(self.scenarios, ensure_ascii=False))
        for text in iter_strings(raw):
            with self.subTest(text=text[:40]):
                self.assertNotRegex(text, r'https?://|www\.|line\.me|@')
                self.assertNotRegex(text, r'\bU[0-9a-f]{32}\b')
                without_dates = re.sub(r'\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(/\d{2,4})?', '', text)
                self.assertNotRegex(without_dates, r'(?<!\d)\+?\d[\d\s().-]{8,}\d')

    # -- projection --------------------------------------------------------------------

    def test_each_scenario_writes_exactly_the_expected_rows_and_notifications(self):
        for scenario in self.scenarios:
            with self.subTest(scenario=scenario['id']):
                self.fresh_spreadsheet()
                expect = scenario['expect']
                if expect.get('raises'):
                    with self.assertRaises(RuntimeError):
                        self.deliver(scenario, f"evt-{scenario['id']}")
                else:
                    self.deliver(scenario, f"evt-{scenario['id']}")

                expected_rows = expect.get('rows', {})
                actual_rows = {name: count for name, count in self.row_counts().items() if count}
                self.assertEqual(expected_rows, actual_rows)

                for sheet, headers in expect.get('cells', {}).items():
                    for header, values in headers.items():
                        self.assertEqual(values, column(self.spreadsheet.sheets[sheet], header))

                messages = '\n'.join(self.notifications())
                if expect.get('notified') is False:
                    self.assertEqual([], self.notifications())
                for needle in expect.get('notify_contains', []):
                    self.assertIn(needle, messages)
                for needle in expect.get('notify_excludes', []):
                    self.assertNotIn(needle, messages)

    def test_redelivery_of_each_scenario_never_duplicates_rows(self):
        for scenario in self.scenarios:
            if scenario['expect'].get('raises'):
                continue
            with self.subTest(scenario=scenario['id']):
                self.fresh_spreadsheet()
                event_id = f"evt-{scenario['id']}"
                self.deliver(scenario, event_id)
                first = self.row_counts()
                self.deliver(scenario, event_id)
                self.assertEqual(first, self.row_counts())

    def test_distinct_events_with_identical_content_are_both_recorded(self):
        scenario = next(s for s in self.scenarios if s['id'] == 'ru_purchase_list')
        self.fresh_spreadsheet()
        self.deliver(scenario, 'evt-first')
        self.deliver(scenario, 'evt-second')
        self.assertEqual({'Закупки': 6}, self.row_counts())

    # -- price drift -------------------------------------------------------------------

    def test_price_increase_over_ten_percent_alerts_once_even_on_redelivery(self):
        base = next(s for s in self.scenarios if s['id'] == 'th_supplier_invoice')
        pricier = dict(base)
        pricier['model_output'] = base['model_output'].replace('"unit_price":"120"', '"unit_price":"138"')
        self.assertNotEqual(base['model_output'], pricier['model_output'])
        self.fresh_spreadsheet()

        self.deliver(base, 'evt-invoice-1')
        self.deliver(pricier, 'evt-invoice-2')
        self.deliver(pricier, 'evt-invoice-2')

        drift = [text for text in self.notifications() if 'ДРЕЙФ ЦЕН' in text]
        self.assertEqual(1, len(drift), drift)
        self.assertIn('Авокадо: 120 → 138 THB (+15%)', drift[0])


if __name__ == '__main__':
    unittest.main()
