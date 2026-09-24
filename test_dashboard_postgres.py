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

    def test_get_tenant_merges_owners_and_ignores_inactive(self):
        self.tenants.upsert_tenant({'id': self.tenant, 'name': 'T', 'country': 'KZ', 'timezone': 'Asia/Almaty', 'status': 'active'})
        self.tenants.upsert_channel({'tenant_id': self.tenant, 'channel': 'whatsapp', 'external_id': self.tenant + '-pn', 'secret_ref': 'x', 'owner_ids': ['a', 'b'], 'allowed_chats': None})
        self.tenants.upsert_channel({'tenant_id': self.tenant, 'channel': 'line', 'external_id': self.tenant + '-ln', 'secret_ref': 'y', 'owner_ids': ['b', 'c'], 'allowed_chats': None})
        tenant = self.tenants.get_tenant(self.tenant)
        self.assertEqual(sorted(tenant['owner_ids']), ['a', 'b', 'c'])
        self.assertIsNone(self.tenants.get_tenant('no-such-tenant'))


if __name__ == '__main__':
    unittest.main()
