"""WhatsApp access requests: the gate, the operator ping, approval into the right list."""

import importlib
import json
import os
import unittest
from types import SimpleNamespace
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

from notimate import access, pipeline  # noqa: E402

CHANNEL = {'channel': 'whatsapp', 'external_id': 'PN', 'secret_ref': 'PN', 'owner_ids': ['owner1'], 'allowed_chats': ['staff1']}


def row(pack='monitor', modules=None):
    return {'tenant': {'id': 't1', 'name': 'Кафе', 'vertical_pack': pack, 'modules': modules or {}}, 'channel': dict(CHANNEL)}


def inbound(sender, text='Кафе Ромашка, отчётность', media=()):
    return SimpleNamespace(sender_id=sender, text=text, media=media, sender_role='owner' if sender == 'owner1' else 'staff', external_event_id='w1')


class KnownSenderTests(unittest.TestCase):
    def test_owner_allowed_chat_and_accountant_are_known(self):
        r = row(modules={'accountant': {'accountant_ids': ['acc1']}})
        for sender in ('owner1', 'staff1', 'acc1'):
            self.assertTrue(access.sender_is_known(r, sender), sender)
        self.assertFalse(access.sender_is_known(r, 'stranger'))

    def test_location_staff_table_counts_for_location_reports_only(self):
        lookup = Mock(return_value={'location_id': 'l1'})
        self.assertTrue(access.sender_is_known(row('location_reports'), 'emp', lookup))
        self.assertFalse(access.sender_is_known(row('monitor'), 'emp', lookup))
        lookup.side_effect = RuntimeError('db')
        self.assertFalse(access.sender_is_known(row('location_reports'), 'emp', lookup))


class GateTests(unittest.TestCase):
    def setUp(self):
        self.send = patch.object(app_module, 'whatsapp_send_text').start()
        self.access_store = Mock()
        self.orig_store = app_module.access_store
        app_module.access_store = self.access_store
        self.addCleanup(setattr, app_module, 'access_store', self.orig_store)
        patch.object(app_module, 'find_whatsapp_channel', side_effect=lambda pnid: row('monitor')).start()
        patch.object(app_module, 'whatsapp_channel_config', return_value={'access_token': 't', 'phone_number_id': 'PN', 'owner_ids': ['owner1']}).start()
        self.monitor = patch.object(app_module, 'process_monitor_event').start()
        patch.dict(os.environ, {'OPERATOR_WHATSAPP_IDS': '7700dev, 7701dev'}).start()
        self.addCleanup(patch.stopall)

    def process(self, sender, text='Кафе Ромашка, отчётность'):
        message = {'from': sender, 'id': 'w1', 'type': 'text', 'text': {'body': text}}
        pipeline.process_whatsapp_event('PN', message)

    def test_unknown_sender_gets_a_request_not_processing(self):
        self.access_store.submit.return_value = (5, True)
        self.process('stranger')
        self.access_store.submit.assert_called_once_with('whatsapp', 'PN', 'stranger', 'Кафе Ромашка, отчётность')
        self.monitor.assert_not_called()
        recipients = [c.args[2] for c in self.send.call_args_list]
        self.assertEqual(recipients, ['stranger', '7700dev', '7701dev'])
        self.assertIn('approve 5', self.send.call_args_list[1].args[3])

    def test_repeat_message_from_pending_sender_does_not_ping_operator_again(self):
        self.access_store.submit.return_value = (5, False)
        self.process('stranger', 'ещё раз')
        self.assertEqual([c.args[2] for c in self.send.call_args_list], ['stranger'])
        self.assertIn('ждёт подтверждения', self.send.call_args.args[3])

    def test_known_sender_goes_to_the_pack(self):
        self.process('staff1', 'молоко 200')
        self.monitor.assert_called_once()
        self.access_store.submit.assert_not_called()

    def test_store_failure_still_answers_the_sender(self):
        self.access_store.submit.side_effect = RuntimeError('db')
        self.process('stranger')
        self.assertEqual(self.send.call_args_list[0].args[2], 'stranger')


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tenants = Mock()
        self.tenants.find_channel.return_value = row('monitor')
        self.reports = Mock()
        self.request = {'channel': 'whatsapp', 'routing_key': 'PN', 'sender_id': '7700111'}

    def test_staff_goes_to_allowed_chats(self):
        result = access.apply_approval(self.tenants, self.reports, self.request, 'staff')
        self.tenants.add_channel_member.assert_called_once_with('whatsapp', 'PN', 'allowed_chats', '7700111')
        self.assertEqual(result['tenant_name'], 'Кафе')

    def test_owner_goes_to_owner_ids(self):
        access.apply_approval(self.tenants, self.reports, self.request, 'owner')
        self.tenants.add_channel_member.assert_called_once_with('whatsapp', 'PN', 'owner_ids', '7700111')

    def test_accountant_is_added_to_module_settings_and_allowed_chats(self):
        self.tenants.get_tenant.return_value = {'id': 't1', 'name': 'Кафе', 'country': 'KZ', 'timezone': 'Asia/Almaty', 'owner_language': 'ru', 'business_type': None,
                                                'vertical_pack': 'monitor', 'sheet_id': 'S', 'custom_context': None, 'status': 'active', 'modules': {'accountant': {'accountant_ids': ['old']}}}
        access.apply_approval(self.tenants, self.reports, self.request, 'accountant')
        saved = self.tenants.upsert_tenant.call_args.args[0]
        self.assertEqual(saved['modules']['accountant']['accountant_ids'], ['old', '7700111'])
        self.assertTrue(saved['modules']['accountant']['enabled'])
        self.tenants.add_channel_member.assert_called_with('whatsapp', 'PN', 'allowed_chats', '7700111')

    def test_location_reports_staff_needs_a_location(self):
        self.tenants.find_channel.return_value = row('location_reports')
        with self.assertRaises(ValueError):
            access.apply_approval(self.tenants, self.reports, self.request, 'staff')
        access.apply_approval(self.tenants, self.reports, self.request, 'staff', location_id='loc1', name='Аня')
        self.reports.upsert_staff.assert_called_once_with('t1', '7700111', 'loc1', 'Аня', 'staff')

    def test_bad_role_and_unknown_number_are_rejected(self):
        with self.assertRaises(ValueError):
            access.apply_approval(self.tenants, self.reports, self.request, 'admin')
        self.tenants.find_channel.return_value = None
        with self.assertRaises(ValueError):
            access.apply_approval(self.tenants, self.reports, self.request, 'staff')


URL = os.environ.get('TEST_DATABASE_URL')


@unittest.skipUnless(URL, 'TEST_DATABASE_URL not set')
class AccessStorePostgresTests(unittest.TestCase):
    def setUp(self):
        import uuid
        self.store = access.PostgresAccessStore(URL)
        self.store.initialize()
        self.pnid = 'PN-' + uuid.uuid4().hex[:8]
        self.addCleanup(self.cleanup)

    def cleanup(self):
        import psycopg
        with psycopg.connect(URL) as conn:
            conn.execute('DELETE FROM access_requests WHERE routing_key = %s', (self.pnid,))

    def test_one_pending_request_per_sender_and_decisions(self):
        first, new1 = self.store.submit('whatsapp', self.pnid, '77001', 'Кафе, отчётность')
        second, new2 = self.store.submit('whatsapp', self.pnid, '77001', 'Кафе Ромашка, отчётность')
        self.assertEqual((first, new1, new2), (second, True, False))
        self.assertEqual(self.store.get(first)['message'], 'Кафе Ромашка, отчётность')  # refreshed with the fuller text
        self.store.submit('whatsapp', self.pnid, '77001', '[фото/файл без текста]')
        self.assertEqual(self.store.get(first)['message'], 'Кафе Ромашка, отчётность')  # placeholder never overwrites
        self.assertIn(first, [r['id'] for r in self.store.list_pending()])
        self.store.decide(first, 'approved', 't1', 'staff')
        self.assertNotIn(first, [r['id'] for r in self.store.list_pending()])
        again, new3 = self.store.submit('whatsapp', self.pnid, '77001', 'снова')  # after a decision a new request may start
        self.assertTrue(new3)
        self.assertNotEqual(again, first)


if __name__ == '__main__':
    unittest.main()
