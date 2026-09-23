"""process_line_event must dual-write every Sheets projection to the new operations_store
tables with the exact same {event_id}:{effect}:{index} key Sheets already uses, so a retried
webhook stays idempotent in both places (Этап 4, docs/21). Sheets/OpenAI/LINE stay mocked —
this only asserts the record_*_safely calls, not Sheets behavior (already covered elsewhere).
"""

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

from test_sheets import FakeGC, FakeSpreadsheet, message_event  # noqa: E402


class DualWriteTests(unittest.TestCase):
    def setUp(self):
        self.spreadsheet = FakeSpreadsheet()
        self.original_gc = app_module.gc
        self.original_sheets_enabled = app_module.SHEETS_ENABLED
        app_module.gc = FakeGC(self.spreadsheet)
        app_module.SHEETS_ENABLED = True
        self.addCleanup(setattr, app_module, 'gc', self.original_gc)
        self.addCleanup(setattr, app_module, 'SHEETS_ENABLED', self.original_sheets_enabled)

        self.record_operation = patch.object(app_module, 'record_operation_safely').start()
        self.record_stock = patch.object(app_module, 'record_stock_signal_safely').start()
        self.record_issue = patch.object(app_module, 'record_issue_safely').start()
        self.record_reminder = patch.object(app_module, 'record_reminder_safely').start()
        patch.object(app_module, 'notify_owner').start()
        patch.object(app_module, 'refresh_overview').start()
        self.addCleanup(patch.stopall)

    def deliver_text(self, event_id, model_output):
        event = message_event(event_id, 'text', 'irrelevant, model output is mocked')
        with patch.object(app_module, 'analyze_text', return_value=model_output):
            app_module.process_line_event('Ubot', event)

    def deliver_image(self, event_id, model_output):
        blob = Mock()
        blob.get_message_content.return_value = b'not-a-real-image'
        event = message_event(event_id, 'image')
        with patch.object(app_module, 'analyze_image', return_value=model_output), \
             patch.object(app_module, 'get_line_blob_api', return_value=blob):
            app_module.process_line_event('Ubot', event)

    # -- text --------------------------------------------------------------------------

    def test_purchase_writes_one_operation_per_item_with_no_amount(self):
        output = json.dumps({'type': 'purchase', 'items': [
            {'product': 'Молоко', 'quantity': '2'}, {'product': 'Лёд', 'quantity': '3'},
        ]}, ensure_ascii=False)
        self.deliver_text('evt-1', output)
        self.assertEqual(self.record_operation.call_count, 2)
        first = self.record_operation.call_args_list[0].args
        self.assertEqual(first[0], 'Ubot')
        self.assertEqual(first[1], 'evt-1:purchase:0')
        self.assertEqual(first[2], 'purchase')
        self.assertIsNone(first[4])  # amount
        self.assertEqual(first[7], 'Молоко')  # description

    def test_stock_writes_one_signal_per_item(self):
        output = json.dumps({'type': 'stock', 'items': [
            {'category': 'Другое', 'product': 'Молоко', 'fridge': '0', 'freezer': '', 'note': 'Out of stock'},
        ]}, ensure_ascii=False)
        self.deliver_text('evt-2', output)
        self.record_stock.assert_called_once_with('Ubot', 'evt-2:stock:0', unittest.mock.ANY, 'Другое', 'Молоко', '0', '', 'Out of stock')

    def test_single_stock_writes_one_signal(self):
        output = json.dumps({'type': 'single_stock', 'product': 'Авокадо', 'amount': '5'}, ensure_ascii=False)
        self.deliver_text('evt-3', output)
        self.record_stock.assert_called_once_with('Ubot', 'evt-3:single-stock:0', unittest.mock.ANY, '', 'Авокадо', '5', '', '')

    def test_text_expense_writes_one_operation_with_parsed_amount(self):
        output = json.dumps({'type': 'text_expense', 'supplier': 'Makro', 'items': [{'description': 'лёд'}], 'total': '฿150'}, ensure_ascii=False)
        self.deliver_text('evt-4', output)
        self.record_operation.assert_called_once_with('Ubot', 'evt-4:text-expense:0', 'expense', unittest.mock.ANY, 150.0, 'THB', 'Makro', 'лёд')

    def test_problem_writes_one_issue(self):
        self.deliver_text('evt-5', 'ВАЖНО [ПРОБЛЕМА]: сломался холодильник\n💡 Совет: вызвать мастера')
        self.record_issue.assert_called_once()
        args = self.record_issue.call_args.args
        self.assertEqual(args[1], 'evt-5:problem:0')

    def test_ignore_writes_nothing(self):
        self.deliver_text('evt-6', 'IGNORE')
        self.record_operation.assert_not_called()
        self.record_stock.assert_not_called()

    # -- image -------------------------------------------------------------------------

    def test_shift_writes_one_revenue_operation(self):
        output = json.dumps({'doc_type': 'shift', 'shift': '1', 'gross_sales': 5000, 'cash': 2000, 'card': 3000, 'qr': 0, 'difference': 0}, ensure_ascii=False)
        self.deliver_image('evt-7', output)
        self.record_operation.assert_called_once_with('Ubot', 'evt-7:shift:0', 'revenue', unittest.mock.ANY, 5000.0, 'THB', None, 'Смена 1', unittest.mock.ANY)

    def test_invoice_writes_one_expense_operation_per_item(self):
        output = json.dumps({'doc_type': 'invoice', 'supplier': 'ACME', 'items': [
            {'description': 'Авокадо', 'unit_price': '120', 'amount': '600'},
            {'description': 'Лимон', 'unit_price': '20', 'amount': '100'},
        ], 'total': '700'}, ensure_ascii=False)
        self.deliver_image('evt-8', output)
        self.assertEqual(self.record_operation.call_count, 2)
        first = self.record_operation.call_args_list[0].args
        self.assertEqual(first[1], 'evt-8:invoice-expense:0')
        self.assertEqual(first[4], 600.0)
        self.assertEqual(first[6], 'ACME')

    def test_salary_writes_one_operation(self):
        output = json.dumps({'doc_type': 'salary', 'recipient': 'Somchai', 'amount': 15000, 'note': ''}, ensure_ascii=False)
        self.deliver_image('evt-9', output)
        self.record_operation.assert_called_once_with('Ubot', 'evt-9:salary:0', 'salary', unittest.mock.ANY, 15000.0, 'THB', 'Somchai', '')

    def test_reminder_writes_one_reminder(self):
        output = json.dumps({'doc_type': 'reminder', 'title': 'Лицензия', 'expiry_date': '2026-10-20', 'note': ''}, ensure_ascii=False)
        self.deliver_image('evt-10', output)
        self.record_reminder.assert_called_once_with('Ubot', 'evt-10:reminder:0', 'Лицензия', '2026-10-20', unittest.mock.ANY, '')

    def test_bank_history_writes_expenses_and_salaries_separately(self):
        output = json.dumps({'doc_type': 'bank_history', 'items': [
            {'type': 'expense', 'recipient': 'Shop', 'amount': 300, 'note': ''},
            {'type': 'salary', 'recipient': 'Somchai', 'amount': 15000, 'note': ''},
        ]}, ensure_ascii=False)
        self.deliver_image('evt-11', output)
        self.assertEqual(self.record_operation.call_count, 2)
        kinds = {call.args[2] for call in self.record_operation.call_args_list}
        self.assertEqual(kinds, {'expense', 'salary'})

    def test_notice_writes_nothing(self):
        output = json.dumps({'doc_type': 'notice', 'title': 'x', 'content': 'y'}, ensure_ascii=False)
        self.deliver_image('evt-12', output)
        self.record_operation.assert_not_called()
        self.record_stock.assert_not_called()
        self.record_issue.assert_not_called()
        self.record_reminder.assert_not_called()

    def test_retry_of_same_event_uses_the_same_event_key(self):
        # idempotency in Postgres comes from a UNIQUE(tenant_id, event_key) constraint, not
        # from pipeline logic — but the pipeline must still send the SAME key on retry.
        output = json.dumps({'type': 'purchase', 'items': [{'product': 'Молоко', 'quantity': '2'}]}, ensure_ascii=False)
        self.deliver_text('evt-13', output)
        self.deliver_text('evt-13', output)
        keys = [call.args[1] for call in self.record_operation.call_args_list]
        self.assertEqual(keys, ['evt-13:purchase:0', 'evt-13:purchase:0'])


if __name__ == '__main__':
    unittest.main()
