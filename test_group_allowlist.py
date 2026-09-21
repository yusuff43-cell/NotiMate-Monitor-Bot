"""process_line_event must honour allowed_group_ids and keep tenants isolated."""

import json
import os
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

from test_sheets import FakeGC, FakeSpreadsheet  # noqa: E402

app_module = importlib.import_module('app')

STOCK_JSON = '{"type":"stock","items":[{"category":"Другое","product":"молоко","amount":"3"}]}'


def group_event(event_id, group_id, text='молоко 3'):
    return {
        'type': 'message',
        'webhookEventId': event_id,
        'source': {'type': 'group', 'groupId': group_id, 'userId': 'Uemployee'},
        'message': {'type': 'text', 'id': f'msg-{event_id}', 'text': text},
    }


def personal_event(event_id, user_id, text='Подключаю кофейню'):
    return {
        'type': 'message',
        'webhookEventId': event_id,
        'source': {'type': 'user', 'userId': user_id},
        'message': {'type': 'text', 'id': f'msg-{event_id}', 'text': text},
    }


class GroupAllowlistProcessingTests(unittest.TestCase):
    def setUp(self):
        self.spreadsheet = FakeSpreadsheet()
        self.original_gc = app_module.gc
        self.original_sheets_enabled = app_module.SHEETS_ENABLED
        self.addCleanup(setattr, app_module, 'gc', self.original_gc)
        self.addCleanup(setattr, app_module, 'SHEETS_ENABLED', self.original_sheets_enabled)
        app_module.gc = FakeGC(self.spreadsheet)
        app_module.SHEETS_ENABLED = True

        base = dict(app_module.CLIENTS['Ubot'])
        self.clients = {
            'Ubot': {**base, 'allowed_group_ids': ['Cwork'], 'owner_line_id_2': 'Unewowner'},
            'Ubot2': {**base, 'sheet_id': 'other-sheet', 'allowed_group_ids': ['Cother']},
        }
        patch.dict(app_module.CLIENTS, self.clients, clear=True).start()
        self.notify = patch.object(app_module, 'notify_owner').start()
        self.analyze_text = patch.object(app_module, 'analyze_text', return_value=STOCK_JSON).start()
        self.analyze_image = patch.object(app_module, 'analyze_image', return_value='NOT_FINANCE').start()
        patch.object(app_module, 'refresh_overview').start()
        patch.object(app_module, 'get_line_blob_api', return_value=Mock()).start()
        self.addCleanup(patch.stopall)

    def total_rows(self):
        return sum(max(len(sheet.rows) - 1, 0) for sheet in self.spreadsheet.sheets.values())

    def test_allowed_group_is_processed(self):
        app_module.process_line_event('Ubot', group_event('evt-ok', 'Cwork'))
        self.analyze_text.assert_called_once()
        self.assertGreater(self.total_rows(), 0)

    def test_unlisted_group_never_reaches_openai_sheets_or_owner(self):
        app_module.process_line_event('Ubot', group_event('evt-dev', 'Cdevtest'))
        self.analyze_text.assert_not_called()
        self.analyze_image.assert_not_called()
        self.notify.assert_not_called()
        self.assertEqual(0, self.total_rows())

    def test_unlisted_group_is_skipped_even_when_sheets_are_down(self):
        app_module.SHEETS_ENABLED = False
        app_module.process_line_event('Ubot', group_event('evt-dev2', 'Cdevtest'))
        self.analyze_text.assert_not_called()

    def test_stranger_personal_message_is_ignored(self):
        app_module.process_line_event('Ubot', personal_event('evt-pm', 'Ustranger'))
        self.analyze_text.assert_not_called()
        self.notify.assert_not_called()
        self.assertEqual(0, self.total_rows())

    def test_second_owner_can_still_use_personal_commands(self):
        with patch.object(app_module, 'owner_menu') as menu:
            app_module.process_line_event('Ubot', personal_event('evt-menu', 'Unewowner', 'меню'))
        menu.assert_called_once()
        self.analyze_text.assert_not_called()

    def test_group_of_another_tenant_is_not_accepted(self):
        app_module.process_line_event('Ubot', group_event('evt-cross', 'Cother'))
        self.analyze_text.assert_not_called()
        self.assertEqual(0, self.total_rows())
        app_module.process_line_event('Ubot2', group_event('evt-own', 'Cother'))
        self.analyze_text.assert_called_once()

    def test_skip_log_contains_no_message_content(self):
        with self.assertLogs('notimate', level='INFO') as logs:
            app_module.process_line_event('Ubot', group_event('evt-log', 'Cdevtest', text='секретная сумма 99999'))
        joined = ' '.join(logs.output)
        self.assertIn('event_skipped_group_not_allowed', joined)
        self.assertNotIn('99999', joined)
        self.assertNotIn('Cdevtest', joined)


if __name__ == '__main__':
    unittest.main()
