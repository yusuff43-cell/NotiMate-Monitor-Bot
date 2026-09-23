"""notimate/projections/operations_store.py: the record_*_safely wrappers must never raise
and must no-op cleanly when operations_store is unavailable (Этап 4, docs/21)."""

import importlib
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

app_module = importlib.import_module('app')

from notimate.projections.operations_store import (  # noqa: E402
    record_issue_safely,
    record_operation_safely,
    record_reminder_safely,
    record_stock_signal_safely,
)


class SafelyWrapperTests(unittest.TestCase):
    def setUp(self):
        self.original_store = app_module.operations_store
        self.addCleanup(setattr, app_module, 'operations_store', self.original_store)

    def test_no_op_when_store_is_none(self):
        app_module.operations_store = None
        # Must not raise even with nonsense args, since it returns before touching them.
        record_operation_safely('Ubot', 'evt:purchase:0', 'purchase', '2026-09-23', None, 'THB', None, 'Молоко')
        record_stock_signal_safely('Ubot', 'evt:stock:0', '2026-09-23', '', 'Молоко', '3', '', '')
        record_issue_safely('Ubot', 'evt:problem:0', '2026-09-23', 'сломался холодильник', 'совет')
        record_reminder_safely('Ubot', 'evt:reminder:0', 'Лицензия', '2026-10-01', '2026-09-23', '')

    def test_calls_through_to_store_when_present(self):
        store = Mock()
        app_module.operations_store = store
        record_operation_safely('Ubot', 'evt:purchase:0', 'purchase', '2026-09-23', None, 'THB', None, 'Молоко')
        store.record_operation.assert_called_once_with('Ubot', 'evt:purchase:0', 'purchase', '2026-09-23', None, 'THB', None, 'Молоко', None)

    def test_store_exception_is_swallowed_and_logged(self):
        store = Mock()
        store.record_operation.side_effect = RuntimeError('connection lost')
        app_module.operations_store = store
        with self.assertLogs('notimate', level='WARNING') as logs:
            record_operation_safely('Ubot', 'evt:purchase:0', 'purchase', '2026-09-23', None, 'THB', None, 'Молоко')
        self.assertIn('operation_record_failed', ' '.join(logs.output))

    def test_stock_signal_exception_is_swallowed(self):
        store = Mock()
        store.record_stock_signal.side_effect = RuntimeError('boom')
        app_module.operations_store = store
        with self.assertLogs('notimate', level='WARNING'):
            record_stock_signal_safely('Ubot', 'evt:stock:0', '2026-09-23', '', 'Молоко', '3', '', '')

    def test_issue_exception_is_swallowed(self):
        store = Mock()
        store.record_issue.side_effect = RuntimeError('boom')
        app_module.operations_store = store
        with self.assertLogs('notimate', level='WARNING'):
            record_issue_safely('Ubot', 'evt:problem:0', '2026-09-23', 'msg', 'advice')

    def test_reminder_exception_is_swallowed(self):
        store = Mock()
        store.record_reminder.side_effect = RuntimeError('boom')
        app_module.operations_store = store
        with self.assertLogs('notimate', level='WARNING'):
            record_reminder_safely('Ubot', 'evt:reminder:0', 'title', '2026-10-01', '2026-09-23', '')


if __name__ == '__main__':
    unittest.main()
