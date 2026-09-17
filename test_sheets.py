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


class FakeWorksheet:
    def __init__(self, headers=None):
        self.rows = [list(headers)] if headers else []
        self.formats = []

    def row_values(self, row):
        return list(self.rows[row - 1]) if len(self.rows) >= row else []

    def update_cell(self, row, column, value):
        while len(self.rows) < row:
            self.rows.append([])
        while len(self.rows[row - 1]) < column:
            self.rows[row - 1].append('')
        self.rows[row - 1][column - 1] = value

    def col_values(self, column):
        values = []
        for row in self.rows:
            values.append(row[column - 1] if len(row) >= column else '')
        while values and values[-1] == '':
            values.pop()
        return values

    def append_row(self, row):
        self.rows.append(list(row))

    def append_rows(self, rows, value_input_option=None):
        self.rows.extend([list(row) for row in rows])

    def get_all_records(self):
        if not self.rows:
            return []
        headers = self.rows[0]
        return [dict(zip(headers, row + [''] * (len(headers) - len(row)))) for row in self.rows[1:] if any(row)]

    def batch_clear(self, ranges):
        self.rows = []

    def update(self, values, range_name=None, value_input_option=None):
        self.rows = [list(row) for row in values]

    def format(self, range_name, style):
        self.formats.append((range_name, style))


class FakeSpreadsheet:
    def __init__(self):
        self.sheets = {}

    def worksheet(self, name):
        if name not in self.sheets:
            raise Exception('worksheet not found')
        return self.sheets[name]

    def add_worksheet(self, title, rows, cols):
        sheet = FakeWorksheet()
        self.sheets[title] = sheet
        return sheet


class FakeGC:
    def __init__(self, spreadsheet):
        self.spreadsheet = spreadsheet

    def open_by_key(self, key):
        return self.spreadsheet


def message_event(event_id, message_type, text=''):
    event = {
        'type': 'message',
        'webhookEventId': event_id,
        'source': {'type': 'group', 'groupId': 'Ctest'},
        'message': {'type': message_type, 'id': f'msg-{event_id}'},
    }
    if text:
        event['message']['text'] = text
    return event


class SheetsIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.spreadsheet = FakeSpreadsheet()
        self.original_gc = app_module.gc
        self.original_sheets_enabled = app_module.SHEETS_ENABLED
        app_module.gc = FakeGC(self.spreadsheet)
        app_module.SHEETS_ENABLED = True
        self.cfg = app_module.CLIENTS['Ubot']

    def tearDown(self):
        app_module.gc = self.original_gc
        app_module.SHEETS_ENABLED = self.original_sheets_enabled

    def test_retry_of_same_event_does_not_add_purchase_rows(self):
        items = [{'product': 'Молоко', 'quantity': '2'}, {'product': 'Лёд', 'quantity': '3'}]
        self.assertEqual(app_module.save_закупки('test-sheet', items, '2026-09-17', 'evt-1'), 2)
        self.assertEqual(app_module.save_закупки('test-sheet', items, '2026-09-17', 'evt-1'), 0)
        ws = self.spreadsheet.worksheet('Закупки')
        self.assertEqual(len(ws.rows), 3)
        self.assertEqual(ws.rows[0][-1], app_module.EVENT_ID_HEADER)

    def test_missing_event_id_refuses_to_write(self):
        with self.assertRaisesRegex(ValueError, 'webhookEventId'):
            app_module.save_проблемы('test-sheet', 'Тест', 'Совет', '2026-09-17', None)

    def test_overview_contains_finances_critical_stock_and_deadlines(self):
        revenue = FakeWorksheet(['Дата', 'Gross Sales'])
        revenue.append_row(['2026-09-17', '1000'])
        expenses = FakeWorksheet(['Дата', 'Сумма (THB)'])
        expenses.append_row(['2026-09-17', '250'])
        stock = FakeWorksheet(['Дата', 'Продукт', 'Холодильник', 'Морозилка', 'Примечание'])
        stock.append_row(['2026-09-17', 'Молоко', '0', '0', 'Out of stock'])
        reminders = FakeWorksheet(['Название', 'Дата окончания'])
        reminders.append_row(['Лицензия', '2026-09-20'])
        self.spreadsheet.sheets.update({'Выручка': revenue, 'Расходы': expenses, 'Остатки': stock, 'Напоминания': reminders})
        with patch.object(app_module.datetime, 'datetime', wraps=app_module.datetime.datetime) as clock:
            clock.now.return_value = app_module.pytz.timezone('Asia/Bangkok').localize(app_module.datetime.datetime(2026, 9, 17, 12, 0))
            app_module.refresh_overview(self.cfg)
        rows = self.spreadsheet.worksheet('Обзор').rows
        flat = ' '.join(str(cell) for row in rows for cell in row)
        self.assertIn('Финансы', flat)
        self.assertIn('Молоко', flat)
        self.assertIn('Лицензия', flat)

    def test_ru_th_en_and_image_document_scenarios_are_projected_once(self):
        text_results = iter([
            '{"type":"purchase","items":[{"product":"Молоко","quantity":"2"}]}',
            '{"type":"stock","items":[{"category":"Другое","product":"Лёд","fridge":"1","freezer":"0","note":"Low stock"}]}',
            '{"type":"text_expense","supplier":"Shop","items":[{"description":"вода"}],"total":"50"}',
            'ВАЖНО [ПРОБЛЕМА]: Холодильник не охлаждает\\n💡 Совет: вызвать техника',
        ])
        image_results = iter([
            '{"doc_type":"expense","supplier":"Market","items":[{"description":"кофе","amount":"120"}],"total":"120","note":""}',
            '{"doc_type":"shift","shift":"2","gross_sales":1000,"cash":400,"card":300,"qr":300,"difference":0,"note":""}',
            '{"doc_type":"reminder","title":"Лицензия","expiry_date":"2026-10-01","note":""}',
        ])
        blob = Mock()
        blob.get_message_content.return_value = b'not-a-real-image-in-unit-test'
        with patch.object(app_module, 'analyze_text', side_effect=text_results), \
             patch.object(app_module, 'analyze_image', side_effect=image_results), \
             patch.object(app_module, 'get_line_blob_api', return_value=blob), \
             patch.object(app_module, 'refresh_overview'), \
             patch.object(app_module, 'notify_owner'):
            # RU purchase, Thai stock, English expense, Russian problem.
            for index, text in enumerate(['нужно молоко 2', 'อัปเดต น้ำแข็ง 1', 'paid 50 for water', 'холодильник не охлаждает']):
                app_module.process_line_event('Ubot', message_event(f'text-{index}', 'text', text))
            # Receipt, shift report and deadline document are exercised through the image path.
            for index in range(3):
                app_module.process_line_event('Ubot', message_event(f'image-{index}', 'image'))
        self.assertIn('Закупки', self.spreadsheet.sheets)
        self.assertIn('Остатки', self.spreadsheet.sheets)
        self.assertIn('Расходы', self.spreadsheet.sheets)
        self.assertIn('Проблемы', self.spreadsheet.sheets)
        self.assertIn('Выручка', self.spreadsheet.sheets)
        self.assertIn('Напоминания', self.spreadsheet.sheets)


if __name__ == '__main__':
    unittest.main()
