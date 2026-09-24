"""Real-PostgreSQL checks for shared WhatsApp numbers (membership routing data, isolation)."""

import os
import subprocess
import sys
import unittest
import uuid

URL = os.environ.get('TEST_DATABASE_URL')


@unittest.skipUnless(URL, 'TEST_DATABASE_URL not set')
class SharedNumberPostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from notimate.packs.location_reports import PostgresLocationReportsStore
        from notimate.tenant_store import PostgresTenantStore
        self.psycopg = psycopg
        self.store = PostgresTenantStore(URL)
        self.store.initialize()
        self.reports = PostgresLocationReportsStore(URL)
        self.reports.initialize()
        self.suffix = uuid.uuid4().hex[:8]
        self.pn = 'shared-' + self.suffix
        self.a, self.b = 'cafe-' + self.suffix, 'shop-' + self.suffix
        for tenant_id, name, pack in ((self.a, 'Кафе', 'monitor'), (self.b, 'Магазин', 'location_reports')):
            self.store.upsert_tenant({'id': tenant_id, 'name': name, 'country': 'KZ', 'timezone': 'Asia/Almaty', 'vertical_pack': pack, 'modules': {}})
        self.store.mark_shared(self.pn, 'REF', 'test')
        self.addCleanup(self.cleanup)

    def cleanup(self):
        with self.psycopg.connect(URL) as conn:
            conn.execute('DELETE FROM sender_context WHERE phone_number_id = %s', (self.pn,))
            conn.execute('DELETE FROM tenant_members WHERE phone_number_id = %s', (self.pn,))
            conn.execute('DELETE FROM staff WHERE tenant_id = ANY(%s)', ([self.a, self.b],))
            conn.execute('DELETE FROM locations WHERE tenant_id = ANY(%s)', ([self.a, self.b],))
            conn.execute("DELETE FROM tenant_channels WHERE external_id = %s", (self.pn,))
            conn.execute('DELETE FROM whatsapp_shared_numbers WHERE phone_number_id = %s', (self.pn,))
            conn.execute('DELETE FROM tenants WHERE id = ANY(%s)', ([self.a, self.b],))

    def test_membership_routes_by_sender_and_is_business_scoped(self):
        self.store.add_member(self.a, self.pn, '7001', 'owner')
        self.store.add_member(self.a, self.pn, '7002', 'staff', 'Аня')
        self.store.add_member(self.b, self.pn, '7002', 'staff', 'Аня', None)
        self.assertEqual([m['tenant_id'] for m in self.store.memberships(self.pn, '7001')], [self.a])
        self.assertEqual(sorted(m['tenant_id'] for m in self.store.memberships(self.pn, '7002')), sorted([self.a, self.b]))
        self.assertEqual(self.store.memberships(self.pn, '7999'), [])
        row_a = self.store.shared_row(self.a, self.pn)
        self.assertEqual((row_a['channel']['owner_ids'], row_a['channel']['allowed_chats']), (['7001'], ['7002']))
        row_b = self.store.shared_row(self.b, self.pn)
        self.assertEqual((row_b['channel']['owner_ids'], row_b['channel']['allowed_chats']), ([], ['7002']))  # no leak of business A's owner
        self.assertIsNone(self.store.shared_row(self.a, 'not-a-shared-number'))

    def test_paused_tenant_does_not_route_and_removed_member_loses_access(self):
        self.store.add_member(self.a, self.pn, '7001', 'owner')
        with self.psycopg.connect(URL) as conn:
            conn.execute("UPDATE tenants SET status = 'paused' WHERE id = %s", (self.a,))
        self.assertEqual(self.store.memberships(self.pn, '7001'), [])
        self.assertIsNone(self.store.shared_row(self.a, self.pn))
        with self.psycopg.connect(URL) as conn:
            conn.execute("UPDATE tenants SET status = 'active' WHERE id = %s", (self.a,))
        self.store.remove_member(self.a, self.pn, '7001')
        self.assertEqual(self.store.memberships(self.pn, '7001'), [])

    def test_context_expires(self):
        self.store.set_context(self.pn, '7002', self.a)
        self.assertEqual(self.store.get_context(self.pn, '7002', 12), self.a)
        self.assertIsNone(self.store.get_context(self.pn, '7002', 0))
        self.store.set_context(self.pn, '7002', self.b)
        self.assertEqual(self.store.get_context(self.pn, '7002', 12), self.b)

    def test_accountant_members_appear_in_module_settings_and_owner_lists(self):
        self.store.add_member(self.a, self.pn, '7003', 'accountant')
        self.store.add_member(self.a, self.pn, '7001', 'owner')
        self.assertEqual(self.store.shared_row(self.a, self.pn)['tenant']['modules']['accountant']['accountant_ids'], ['7003'])
        tenant = self.store.get_tenant(self.a)
        self.assertEqual(tenant['owner_ids'], ['7001'])
        self.assertEqual(tenant['modules']['accountant']['accountant_ids'], ['7003'])
        self.assertIn('whatsapp', tenant['channels'])

    def test_list_channels_includes_one_row_per_business_on_shared_numbers(self):
        self.store.add_member(self.a, self.pn, '7001', 'owner')
        self.store.add_member(self.b, self.pn, '7001', 'owner')
        ids = sorted(r['tenant']['id'] for r in self.store.list_channels('whatsapp') if r['channel']['external_id'] == self.pn)
        self.assertEqual(ids, sorted([self.a, self.b]))
        self.assertTrue(all(r['channel'].get('shared') for r in self.store.list_channels('whatsapp') if r['channel']['external_id'] == self.pn))

    def test_group_membership_only_lists_businesses_the_person_owns(self):
        self.assertTrue(self.store.patch_tenant(self.a, modules_patch={'group': 'g-' + self.suffix}))
        self.assertTrue(self.store.patch_tenant(self.b, modules_patch={'group': 'g-' + self.suffix, 'extra': 1}))
        self.assertFalse(self.store.patch_tenant('no-such', name='x'))
        self.store.add_member(self.a, self.pn, '7001', 'owner')
        self.store.add_member(self.b, self.pn, '7001', 'owner')
        self.store.add_member(self.b, self.pn, '7002', 'owner')
        self.store.add_member(self.a, self.pn, '7003', 'staff')
        group = 'g-' + self.suffix
        self.assertEqual(sorted(t['id'] for t in self.store.group_tenants(group, '7001')), sorted([self.a, self.b]))
        self.assertEqual([t['id'] for t in self.store.group_tenants(group, '7002')], [self.b])  # owns one of two
        self.assertEqual(self.store.group_tenants(group, '7003'), [])  # staff is not an owner
        self.assertEqual(len(self.store.group_tenants(group)), 2)
        self.assertIn(group, self.store.list_groups())
        self.assertEqual(self.store.get_tenant(self.b)['modules']['extra'], 1)  # patch merged, not replaced
        self.store.patch_tenant(self.a, vertical_pack='location_reports', sheet_id='SHEET', modules_patch={'extra_packs': ['monitor']})
        tenant = self.store.get_tenant(self.a)
        self.assertEqual((tenant['vertical_pack'], tenant['sheet_id'], tenant['modules']['group']), ('location_reports', 'SHEET', group))

    def test_make_number_shared_moves_owner_staff_and_location_staff(self):
        dedicated = 'dedicated-' + self.suffix
        self.store.upsert_channel({'tenant_id': self.b, 'channel': 'whatsapp', 'external_id': dedicated, 'secret_ref': 'REF2', 'owner_ids': ['8001'], 'allowed_chats': ['8002']})
        self.reports.upsert_location(self.b, 'loc-' + self.suffix, 'Точка 1')
        self.reports.upsert_staff(self.b, '8003', 'loc-' + self.suffix, 'Аня')
        script = os.path.join(os.path.dirname(__file__), 'deploy', 'make_number_shared.py')
        env = {**os.environ, 'DATABASE_URL': URL}
        dry = subprocess.run([sys.executable, script, '--phone-number-id', dedicated, '--dry-run'], env=env, capture_output=True, text=True)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIsNotNone(self.store.find_channel('whatsapp', dedicated))  # dry run changed nothing
        done = subprocess.run([sys.executable, script, '--phone-number-id', dedicated], env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIsNone(self.store.find_channel('whatsapp', dedicated))
        self.assertEqual(self.store.shared_number(dedicated)['secret_ref'], 'REF2')
        roles = {m: self.store.memberships(dedicated, m)[0]['role'] for m in ('8001', '8002', '8003')}
        self.assertEqual(roles, {'8001': 'owner', '8002': 'staff', '8003': 'staff'})
        self.assertEqual(self.store.memberships(dedicated, '8003')[0]['location_id'], 'loc-' + self.suffix)
        again = subprocess.run([sys.executable, script, '--phone-number-id', dedicated], env=env, capture_output=True, text=True)
        self.assertIn('Already a shared number', again.stdout)
        with self.psycopg.connect(URL) as conn:
            conn.execute('DELETE FROM tenant_members WHERE phone_number_id = %s', (dedicated,))
            conn.execute('DELETE FROM whatsapp_shared_numbers WHERE phone_number_id = %s', (dedicated,))


if __name__ == '__main__':
    unittest.main()
