"""app.find_client must prefer the tenants/tenant_channels store when it has a usable row,
fall back to CLIENTS_JSON otherwise, and never let two tenants' data cross (Этап 2, docs/21).
"""

import importlib
import json
import os
import unittest
from unittest.mock import patch

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

# app.py's CLIENTS reflects whichever test module's CLIENTS_JSON the shared `app` singleton
# saw first (os.environ.setdefault is a no-op after that). Every test below replaces
# app_module.CLIENTS via patch.dict instead of relying on that env var's content, exactly
# like test_group_allowlist.py already does for the same reason.
CLIENTS_UNDER_TEST = {
    'Ubot1': {
        'channel_access_token': 'token-1',
        'channel_secret': 'secret-1',
        'owner_line_id': 'Uowner1',
        'sheet_id': 'sheet-1',
        'name': 'Cafe One',
    },
    'Ubot2': {
        'channel_access_token': 'token-2',
        'channel_secret': 'secret-2',
        'owner_line_id': 'Uowner2',
        'sheet_id': 'sheet-2',
        'name': 'Cafe Two',
    },
}


class FakeTenantStore:
    def __init__(self, rows=None, raise_on_lookup=False):
        self._rows = rows or {}
        self.raise_on_lookup = raise_on_lookup

    def find_channel(self, channel, external_id):
        if self.raise_on_lookup:
            raise RuntimeError('connection lost')
        return self._rows.get((channel, external_id))


def tenant_channel_row(tenant_id, sheet_id, owner_ids, secret_ref, allowed_chats=None, **tenant_extra):
    return {
        'tenant': {
            'id': tenant_id,
            'sheet_id': sheet_id,
            'name': tenant_extra.get('name', tenant_id),
            'business_type': tenant_extra.get('business_type'),
            'custom_context': tenant_extra.get('custom_context'),
        },
        'channel': {
            'channel': 'line',
            'external_id': tenant_id,
            'secret_ref': secret_ref,
            'owner_ids': owner_ids,
            'allowed_chats': allowed_chats,
        },
    }


class FindClientTenantStoreTests(unittest.TestCase):
    def setUp(self):
        self.original_tenant_store = app_module.tenant_store
        self.original_db_enabled = app_module.TENANTS_DB_ENABLED
        self.addCleanup(setattr, app_module, 'tenant_store', self.original_tenant_store)
        self.addCleanup(setattr, app_module, 'TENANTS_DB_ENABLED', self.original_db_enabled)
        patch.dict(app_module.CLIENTS, CLIENTS_UNDER_TEST, clear=True).start()
        self.addCleanup(patch.stopall)

    def use_store(self, store):
        app_module.tenant_store = store
        app_module.TENANTS_DB_ENABLED = True

    def test_falls_back_to_clients_json_when_tenant_store_disabled(self):
        app_module.tenant_store = None
        app_module.TENANTS_DB_ENABLED = False
        self.assertEqual(app_module.find_client('Ubot1'), app_module.CLIENTS['Ubot1'])

    def test_falls_back_to_clients_json_when_no_tenant_row(self):
        self.use_store(FakeTenantStore(rows={}))
        self.assertEqual(app_module.find_client('Ubot1'), app_module.CLIENTS['Ubot1'])

    def test_falls_back_when_secret_ref_is_incomplete(self):
        row = tenant_channel_row('Ubot1', sheet_id='sheet-1', owner_ids=['Uowner1'], secret_ref='missing-destination')
        self.use_store(FakeTenantStore(rows={('line', 'Ubot1'): row}))
        with self.assertLogs('notimate', level='WARNING') as logs:
            cfg = app_module.find_client('Ubot1')
        self.assertEqual(cfg, app_module.CLIENTS['Ubot1'])
        self.assertIn('tenant_channel_config_incomplete', ' '.join(logs.output))

    def test_falls_back_when_tenant_row_has_no_sheet_id(self):
        row = tenant_channel_row('Ubot1', sheet_id=None, owner_ids=['Uowner1'], secret_ref='Ubot1')
        self.use_store(FakeTenantStore(rows={('line', 'Ubot1'): row}))
        self.assertEqual(app_module.find_client('Ubot1'), app_module.CLIENTS['Ubot1'])

    def test_lookup_failure_falls_back_safely(self):
        self.use_store(FakeTenantStore(raise_on_lookup=True))
        with self.assertLogs('notimate', level='WARNING') as logs:
            cfg = app_module.find_client('Ubot1')
        self.assertEqual(cfg, app_module.CLIENTS['Ubot1'])
        self.assertIn('tenant_lookup_failed', ' '.join(logs.output))

    def test_resolves_from_tenant_store_when_row_is_complete(self):
        row = tenant_channel_row(
            'Ubot1', sheet_id='sheet-1', owner_ids=['Uowner1'], secret_ref='Ubot1',
            allowed_chats=['Cwork'], name='Cafe One (DB)', business_type='cafe',
        )
        self.use_store(FakeTenantStore(rows={('line', 'Ubot1'): row}))
        cfg = app_module.find_client('Ubot1')
        self.assertEqual(cfg['channel_access_token'], 'token-1')
        self.assertEqual(cfg['channel_secret'], 'secret-1')
        self.assertEqual(cfg['owner_line_id'], 'Uowner1')
        self.assertEqual(cfg['sheet_id'], 'sheet-1')
        self.assertEqual(cfg['name'], 'Cafe One (DB)')
        self.assertEqual(cfg['allowed_group_ids'], ['Cwork'])

    def test_two_tenants_on_different_line_destinations_never_cross(self):
        row1 = tenant_channel_row('Ubot1', sheet_id='sheet-1', owner_ids=['Uowner1'], secret_ref='Ubot1', name='Cafe One (DB)')
        row2 = tenant_channel_row('Ubot2', sheet_id='sheet-2', owner_ids=['Uowner2'], secret_ref='Ubot2', name='Cafe Two (DB)')
        self.use_store(FakeTenantStore(rows={('line', 'Ubot1'): row1, ('line', 'Ubot2'): row2}))
        cfg1 = app_module.find_client('Ubot1')
        cfg2 = app_module.find_client('Ubot2')
        self.assertEqual(cfg1['sheet_id'], 'sheet-1')
        self.assertEqual(cfg2['sheet_id'], 'sheet-2')
        self.assertEqual(cfg1['channel_access_token'], 'token-1')
        self.assertEqual(cfg2['channel_access_token'], 'token-2')
        self.assertNotEqual(cfg1['owner_line_id'], cfg2['owner_line_id'])

    def test_channel_and_external_id_together_form_the_isolation_key(self):
        # Same raw external_id string on two different channels must resolve to two
        # completely independent tenants: the composite (channel, external_id) key is
        # what tenant_channels enforces (PRIMARY KEY (channel, external_id) in the schema),
        # not external_id alone.
        line_row = tenant_channel_row('shared-id', sheet_id='sheet-line', owner_ids=['Uowner1'], secret_ref='Ubot1', name='Line tenant')
        whatsapp_row = {
            'tenant': {'id': 'other-tenant', 'sheet_id': 'sheet-whatsapp', 'name': 'WhatsApp tenant', 'business_type': None, 'custom_context': None},
            'channel': {'channel': 'whatsapp', 'external_id': 'shared-id', 'secret_ref': 'Ubot2', 'owner_ids': ['Uowner2'], 'allowed_chats': None},
        }
        store = FakeTenantStore(rows={
            ('line', 'shared-id'): line_row,
            ('whatsapp', 'shared-id'): whatsapp_row,
        })
        self.assertEqual(store.find_channel('line', 'shared-id')['tenant']['sheet_id'], 'sheet-line')
        self.assertEqual(store.find_channel('whatsapp', 'shared-id')['tenant']['sheet_id'], 'sheet-whatsapp')


if __name__ == '__main__':
    unittest.main()
