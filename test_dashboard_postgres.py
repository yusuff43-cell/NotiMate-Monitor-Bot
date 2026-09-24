"""Real-PostgreSQL checks for the dashboard reader and tenant lookup.

  TEST_DATABASE_URL='postgresql:///notimate_test?host=/tmp' python -m unittest test_dashboard_postgres
"""

import datetime as dt
import os
import unittest
import uuid

URL = os.environ.get('TEST_DATABASE_URL')


@unittest.skipUnless(URL, 'TEST_DATABASE_URL not set')
class DashboardReaderPostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from notimate.dashboard.reader import PostgresDashboardReader
        from notimate.packs.location_reports import PostgresLocationReportsStore
        from notimate.projections.operations_store import PostgresOperationsStore
        from notimate.tenant_store import PostgresTenantStore
        self.psycopg = psycopg
        self.tenant = 'dash-' + uuid.uuid4().hex[:8]
        self.other = 'dash-' + uuid.uuid4().hex[:8]
        self.ops = PostgresOperationsStore(URL)
        self.ops.initialize()
        self.locations = PostgresLocationReportsStore(URL)
        self.locations.initialize()
        self.tenants = PostgresTenantStore(URL)
        self.tenants.initialize()
        self.reader = PostgresDashboardReader(URL)
        self.today = dt.date(2026, 9, 24)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        with self.psycopg.connect(URL) as conn:
            for table in ('operations', 'stock_signals', 'issues', 'reminders', 'location_reports', 'location_report_drafts', 'staff', 'locations', 'tenant_channels'):
                column = 'tenant_id'
                conn.execute(f'DELETE FROM {table} WHERE {column} = ANY(%s)', ([self.tenant, self.other],))
            conn.execute('DELETE FROM tenants WHERE id = ANY(%s)', ([self.tenant, self.other],))

    def test_totals_are_tenant_scoped_and_expense_definition_matches_summary(self):
        self.ops.record_operation(self.tenant, 'r1', 'revenue', '2026-09-24', 1000, 'THB', None, 'Смена 1', {'cash': 400, 'card': '500', 'qr': 100})
        self.ops.record_operation(self.tenant, 'e1', 'expense', '2026-09-24', 300, 'THB', 'Makro', 'milk')
        self.ops.record_operation(self.tenant, 's1', 'salary', '2026-09-24', 700, 'THB', 'Ann', '')
        self.ops.record_operation(self.tenant, 'p1', 'purchase', '2026-09-24', None, 'THB', None, 'avocado')
        self.ops.record_operation(self.other, 'r2', 'revenue', '2026-09-24', 77777, 'THB', None, 'other tenant')
        totals = self.reader.daily_totals(self.tenant, self.today - dt.timedelta(days=5), self.today, False)
        self.assertEqual(totals, {'2026-09-24': {'revenue': 1000.0, 'expenses': 300.0}})  # salary excluded, like «Расходы»
        self.assertEqual(self.reader.payments_for(self.tenant, self.today, False), {'cash': 400.0, 'card': 500.0, 'qr': 100.0})
        recent = self.reader.recent_operations(self.tenant, False)
        self.assertEqual({r['label'] for r in recent}, {'Смена 1', 'milk', 'Ann'})

    def test_stock_deadlines_problems(self):
        self.ops.record_stock_signal(self.tenant, 'k1', '2026-09-23', 'Десерты', 'Баунти', '2 шт', '', 'Low stock')
        self.ops.record_stock_signal(self.tenant, 'k2', '2026-09-24', 'Десерты', 'Баунти', '0', '', 'Out of stock')
        self.ops.record_stock_signal(self.tenant, 'k3', '2026-09-24', '', 'Сыр', '5', '', '')
        self.ops.record_reminder(self.tenant, 'q1', 'Лицензия', '2026-09-27', '2026-09-01', '')
        self.ops.record_reminder(self.tenant, 'q2', 'Далеко', '2027-01-01', '2026-09-01', '')
        self.ops.record_issue(self.tenant, 'i1', '2026-09-23', 'Сломался кондиционер', 'вызвать мастера')
        stock = self.reader.critical_stock(self.tenant, self.today - dt.timedelta(days=3))
        self.assertEqual(stock, [{'product': 'Баунти', 'amount': '0', 'status': 'out'}])  # latest signal wins, plain rows ignored
        deadlines = self.reader.deadlines(self.tenant, self.today)
        self.assertEqual([(d['title'], d['daysLeft']) for d in deadlines], [('Лицензия', 3)])
        self.assertEqual(self.reader.problems(self.tenant, self.today - dt.timedelta(days=7))[0]['title'], 'Сломался кондиционер')

    def test_location_pack_reads_location_reports(self):
        self.locations.upsert_location(self.tenant, self.tenant + '-l1', 'Точка 1')
        self.locations.upsert_staff(self.tenant, 's1', self.tenant + '-l1', 'Аня')
        draft = self.locations.create_draft(self.tenant, self.tenant + '-l1', 's1', '2026-09-24', {'revenue': 50000, 'cash': 20000, 'non_cash': 30000, 'external_payouts': 4000, 'cash_balance': 1000, 'comment': ''}, 'x')
        self.locations.confirm_draft(draft)
        totals = self.reader.daily_totals(self.tenant, self.today, self.today, True)
        self.assertEqual(totals['2026-09-24'], {'revenue': 50000.0, 'expenses': 4000.0})
        self.assertEqual(self.reader.payments_for(self.tenant, self.today, True), {'cash': 20000.0, 'card': 30000.0, 'qr': 0.0})
        self.assertEqual(self.reader.recent_operations(self.tenant, True)[0]['label'], 'Точка 1')

    def test_expense_rows_and_location_rows(self):
        self.ops.record_operation(self.tenant, 'e1', 'expense', '2026-09-24', 300, 'THB', 'Makro', 'milk')
        self.ops.record_operation(self.tenant, 's1', 'salary', '2026-09-23', 700, 'THB', 'Ann', '')
        self.ops.record_operation(self.tenant, 'r1', 'revenue', '2026-09-24', 9000, 'THB', None, 'shift')
        self.ops.record_operation(self.tenant, 'old', 'expense', '2026-08-01', 5, 'THB', 'X', 'old')
        self.ops.record_operation(self.other, 'e2', 'expense', '2026-09-24', 99999, 'THB', 'Other', 'other tenant')
        rows = self.reader.expense_rows(self.tenant, dt.date(2026, 9, 1), self.today, False)
        self.assertEqual([(r['operation_type'], float(r['amount'])) for r in rows], [('expense', 300.0), ('salary', 700.0)])
        self.assertEqual(self.reader.expense_rows(self.tenant, dt.date(2026, 9, 1), self.today, True), [])
        self.locations.upsert_location(self.tenant, self.tenant + '-l1', 'Точка 1')
        self.locations.upsert_location(self.tenant, self.tenant + '-l2', 'Точка 2')
        self.locations.upsert_staff(self.tenant, 's1', self.tenant + '-l1', 'Аня')
        draft = self.locations.create_draft(self.tenant, self.tenant + '-l1', 's1', '2026-09-24', {'revenue': 1000, 'cash': 400, 'non_cash': 600, 'external_payouts': 10, 'cash_balance': 1, 'comment': ''}, 'x')
        self.locations.confirm_draft(draft)
        by_name = {r['name']: r for r in self.reader.location_rows(self.tenant, self.today, self.today)}
        self.assertEqual(float(by_name['Точка 1']['revenue']), 1000.0)
        self.assertEqual(int(by_name['Точка 1']['reports']), 1)
        self.assertEqual((float(by_name['Точка 2']['revenue']), int(by_name['Точка 2']['reports'])), (0.0, 0))  # unreported point still listed

    def test_get_tenant_merges_owners_and_ignores_inactive(self):
        self.tenants.upsert_tenant({'id': self.tenant, 'name': 'T', 'country': 'KZ', 'timezone': 'Asia/Almaty', 'status': 'active'})
        self.tenants.upsert_channel({'tenant_id': self.tenant, 'channel': 'whatsapp', 'external_id': self.tenant + '-pn', 'secret_ref': 'x', 'owner_ids': ['a', 'b'], 'allowed_chats': None})
        self.tenants.upsert_channel({'tenant_id': self.tenant, 'channel': 'line', 'external_id': self.tenant + '-ln', 'secret_ref': 'y', 'owner_ids': ['b', 'c'], 'allowed_chats': None})
        tenant = self.tenants.get_tenant(self.tenant)
        self.assertEqual(sorted(tenant['owner_ids']), ['a', 'b', 'c'])
        self.assertEqual(tenant['channels'], ['line', 'whatsapp'])
        pn = self.tenant + '-pn'
        self.tenants.add_channel_member('whatsapp', pn, 'allowed_chats', '7700')
        self.tenants.add_channel_member('whatsapp', pn, 'allowed_chats', '7700')  # idempotent
        self.tenants.add_channel_member('whatsapp', pn, 'owner_ids', 'z')
        row = self.tenants.find_channel('whatsapp', pn)
        self.assertEqual(row['channel']['allowed_chats'], ['7700'])
        self.assertEqual(row['channel']['owner_ids'], ['a', 'b', 'z'])
        with self.assertRaises(ValueError):
            self.tenants.add_channel_member('whatsapp', pn, 'secret_ref', 'x')
        self.assertIsNone(self.tenants.get_tenant('no-such-tenant'))


class FakeWorksheet:
    def __init__(self, rows=None, error=None):
        self.rows, self.error = rows or [], error

    def get_all_records(self):
        if self.error:
            raise self.error
        return self.rows


class FakeSpreadsheet:
    def __init__(self, tabs):
        self.tabs = tabs

    def worksheet(self, name):
        if name not in self.tabs:
            class WorksheetNotFound(Exception):
                pass
            raise WorksheetNotFound(name)
        return self.tabs[name]


class FakeGC:
    def __init__(self, tabs):
        self.sheet = FakeSpreadsheet(tabs)

    def open_by_key(self, key):
        return self.sheet


@unittest.skipUnless(URL, 'TEST_DATABASE_URL not set')
class SheetsSyncPostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from notimate.projections.operations_store import PostgresOperationsStore
        self.psycopg = psycopg
        self.tenant = 'sync-' + uuid.uuid4().hex[:8]
        self.ops = PostgresOperationsStore(URL)
        self.ops.initialize()
        self.today = dt.date(2026, 9, 24)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        with self.psycopg.connect(URL) as conn:
            for table in ('operations', 'stock_signals', 'issues', 'reminders'):
                conn.execute(f'DELETE FROM {table} WHERE tenant_id = %s', (self.tenant,))

    def sync(self, tabs, **kw):
        from notimate.projections.sheets_sync import sync_tenant
        return sync_tenant(self.tenant, {'sheet_id': 'x'}, database_url=URL, gc=FakeGC(tabs), today=self.today, **kw)

    def rows(self, table='operations'):
        with self.psycopg.connect(URL) as conn:
            extra = ', status' if table == 'operations' else ''
            cols = {'operations': 'event_key, amount, description', 'stock_signals': 'event_key, product, note', 'issues': 'event_key, message', 'reminders': 'event_key, title'}[table]
            return conn.execute(f'SELECT {cols}{extra} FROM {table} WHERE tenant_id = %s ORDER BY event_key', (self.tenant,)).fetchall()

    def expense_rows(self):
        return [
            {'Дата': '2026-09-20', 'Поставщик/Магазин': 'Makro', 'Позиция': 'milk', 'Сумма (THB)': 100, 'NotiMate Event ID': 'evtA:invoice-expense:0'},
            {'Дата': '2026-09-21', 'Поставщик/Магазин': 'Market', 'Позиция': 'eggs', 'Сумма (THB)': 60},
        ]

    def test_initial_sync_is_idempotent_and_reuses_event_keys(self):
        self.ops.record_operation(self.tenant, 'evtA:invoice-expense:0', 'expense', '2026-09-20', 100, 'THB', 'Makro', 'milk')  # dual-write row
        first = self.sync({'Расходы': FakeWorksheet(self.expense_rows()), 'Остатки': FakeWorksheet([{'Дата': '2026-09-20', 'Продукт': 'Сыр', 'Холодильник': 2, 'Примечание': 'Low stock'}])})
        self.assertEqual((first['inserted'], first['updated'], first['removed']), (2, 0, 0))  # dual-write row untouched, not duplicated
        self.assertEqual(len(self.rows()), 2)
        again = self.sync({'Расходы': FakeWorksheet(self.expense_rows()), 'Остатки': FakeWorksheet([{'Дата': '2026-09-20', 'Продукт': 'Сыр', 'Холодильник': 2, 'Примечание': 'Low stock'}])})
        self.assertEqual((again['inserted'], again['updated'], again['removed']), (0, 0, 0))

    def test_numeric_looking_text_does_not_cause_endless_updates(self):
        rows = [{'Дата': '2026-09-20', 'Поставщик/Магазин': 'S', 'Позиция': 460, 'Сумма (THB)': 460}]
        self.sync({'Расходы': FakeWorksheet(rows)})
        again = self.sync({'Расходы': FakeWorksheet(rows)})
        self.assertEqual((again['inserted'], again['updated'], again['removed']), (0, 0, 0))

    def test_manual_edit_of_an_event_keyed_row_updates_it_in_place(self):
        self.sync({'Расходы': FakeWorksheet(self.expense_rows())})
        edited = self.expense_rows()
        edited[0]['Сумма (THB)'] = 150
        result = self.sync({'Расходы': FakeWorksheet(edited)})
        self.assertEqual(result['updated'], 1)
        self.assertEqual(float(dict((r[0], r[1]) for r in self.rows())['evtA:invoice-expense:0']), 150.0)

    def test_editing_or_deleting_a_handtyped_row_replaces_or_rejects_it(self):
        self.sync({'Расходы': FakeWorksheet(self.expense_rows())})
        edited = self.expense_rows()
        edited[1]['Сумма (THB)'] = 65
        result = self.sync({'Расходы': FakeWorksheet(edited)})
        self.assertEqual((result['inserted'], result['removed']), (1, 1))
        confirmed = [r for r in self.rows() if r[3] == 'confirmed']
        self.assertEqual(sorted(float(r[1]) for r in confirmed), [65.0, 100.0])
        deleted = self.sync({'Расходы': FakeWorksheet(edited[:1])})
        self.assertEqual(deleted['removed'], 1)
        self.assertEqual(len([r for r in self.rows() if r[3] == 'confirmed']), 1)

    def test_deleted_rows_are_revived_when_they_return(self):
        self.sync({'Расходы': FakeWorksheet(self.expense_rows())})
        self.sync({'Расходы': FakeWorksheet(self.expense_rows()[1:])})
        self.assertEqual(len([r for r in self.rows() if r[3] == 'confirmed']), 1)
        back = self.sync({'Расходы': FakeWorksheet(self.expense_rows())})
        self.assertEqual(back['updated'], 1)
        self.assertEqual(len([r for r in self.rows() if r[3] == 'confirmed']), 2)

    def test_unreadable_tab_never_deletes(self):
        self.sync({'Расходы': FakeWorksheet(self.expense_rows())})
        result = self.sync({'Расходы': FakeWorksheet(error=RuntimeError('quota'))})
        self.assertEqual((result['removed'], result['inserted']), (0, 0))
        self.assertEqual(len([r for r in self.rows() if r[3] == 'confirmed']), 2)

    def test_mass_delete_is_refused(self):
        many = [{'Дата': '2026-09-20', 'Поставщик/Магазин': 'S', 'Позиция': f'item{i}', 'Сумма (THB)': i + 1} for i in range(30)]
        self.sync({'Расходы': FakeWorksheet(many)})
        result = self.sync({'Расходы': FakeWorksheet([])})  # e.g. a blank/broken read
        self.assertEqual(result['removed'], 0)
        self.assertEqual(result['skipped_removals'], 30)
        self.assertEqual(len([r for r in self.rows() if r[3] == 'confirmed']), 30)

    def test_legacy_backfill_keys_are_replaced(self):
        self.ops.record_operation(self.tenant, 'backfill:Расходы:2', 'expense', '2026-09-21', 60, 'THB', 'Market', 'eggs')
        self.sync({'Расходы': FakeWorksheet(self.expense_rows())})
        keys = [r[0] for r in self.rows()]
        self.assertFalse(any(k.startswith('backfill:') for k in keys))
        self.assertEqual(len(keys), 2)

    def test_dry_run_writes_nothing(self):
        result = self.sync({'Расходы': FakeWorksheet(self.expense_rows())}, dry_run=True)
        self.assertTrue(result['dry_run'])
        self.assertEqual(self.rows(), [])


if __name__ == '__main__':
    unittest.main()
