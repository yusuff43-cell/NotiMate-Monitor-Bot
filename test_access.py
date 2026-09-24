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
        patch.object(app_module, 'whatsapp_send_proactive', self.send).start()  # bot-initiated pings share the mock
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
        self.tenants.shared_number.return_value = None
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


class SharedApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tenants = Mock()
        self.tenants.shared_number.return_value = {'phone_number_id': 'PN', 'secret_ref': 'PN'}
        self.tenants.get_tenant.return_value = {'id': 'cafe', 'name': 'Кафе', 'vertical_pack': 'monitor'}
        self.reports = Mock()
        self.request = {'channel': 'whatsapp', 'routing_key': 'PN', 'sender_id': '7700111'}

    def test_operator_names_the_business_and_member_is_added_there_only(self):
        result = access.apply_approval(self.tenants, self.reports, self.request, 'staff', tenant_id='cafe')
        self.tenants.add_member.assert_called_once_with('cafe', 'PN', '7700111', 'staff', '', None)
        self.assertEqual(result['tenant_name'], 'Кафе')

    def test_tenant_is_required_and_must_exist(self):
        with self.assertRaises(ValueError):
            access.apply_approval(self.tenants, self.reports, self.request, 'staff')
        self.tenants.get_tenant.return_value = None
        with self.assertRaises(ValueError):
            access.apply_approval(self.tenants, self.reports, self.request, 'staff', tenant_id='ghost')
        self.tenants.add_member.assert_not_called()

    def test_location_staff_needs_a_point_and_is_registered_in_the_staff_table(self):
        self.tenants.get_tenant.return_value = {'id': 'erzhan', 'name': 'Ержан', 'vertical_pack': 'location_reports'}
        with self.assertRaises(ValueError):
            access.apply_approval(self.tenants, self.reports, self.request, 'staff', tenant_id='erzhan')
        access.apply_approval(self.tenants, self.reports, self.request, 'staff', tenant_id='erzhan', location_id='loc1', name='Аня')
        self.reports.upsert_staff.assert_called_once_with('erzhan', '7700111', 'loc1', 'Аня', 'staff')
        self.tenants.add_member.assert_called_once_with('erzhan', 'PN', '7700111', 'staff', 'Аня', 'loc1')


class SharedRoutingTests(unittest.TestCase):
    """resolve_shared: the sender's membership — never their text — picks the business."""

    def setUp(self):
        self.store = Mock()
        self.store.shared_number.return_value = {'phone_number_id': 'PN', 'secret_ref': 'PN'}
        self.store.get_context.return_value = None
        self.store.shared_row.side_effect = lambda tenant_id, pnid: {'tenant': {'id': tenant_id, 'name': tenant_id}, 'channel': {'shared': True, 'external_id': pnid}}
        self.orig = (app_module.tenant_store, app_module.access_store, dict(app_module.WHATSAPP_SECRETS))
        app_module.tenant_store, app_module.access_store = self.store, Mock()
        app_module.access_store.submit.return_value = (9, True)
        app_module.WHATSAPP_SECRETS['PN'] = {'access_token': 'tok'}
        self.send = patch.object(app_module, 'whatsapp_send_text').start()
        patch.object(app_module, 'whatsapp_send_proactive', self.send).start()
        self.buttons = patch.object(app_module, 'whatsapp_send_interactive_buttons').start()
        patch.dict(os.environ, {'OPERATOR_WHATSAPP_IDS': 'op1'}).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(lambda: (setattr(app_module, 'tenant_store', self.orig[0]), setattr(app_module, 'access_store', self.orig[1]), app_module.WHATSAPP_SECRETS.clear(), app_module.WHATSAPP_SECRETS.update(self.orig[2])))

    def resolve(self, sender, text='молоко 200'):
        from notimate.shared_number import resolve_shared
        return resolve_shared('PN', {'from': sender, 'id': 'w', 'type': 'text', 'text': {'body': text}})

    def member(self, tenant_id, name=None):
        return {'tenant_id': tenant_id, 'tenant_name': name or tenant_id, 'role': 'staff', 'name': '', 'location_id': None}

    def test_not_a_shared_number(self):
        self.store.shared_number.return_value = None
        self.assertEqual(self.resolve('7700'), (None, 'unknown'))

    def test_single_membership_routes_to_that_business(self):
        self.store.memberships.return_value = [self.member('cafe')]
        row, outcome = self.resolve('7700')
        self.assertEqual((row['tenant']['id'], outcome), ('cafe', None))
        self.store.shared_row.assert_called_once_with('cafe', 'PN')

    def test_text_naming_another_business_never_switches_business(self):
        self.store.memberships.return_value = [self.member('cafe')]
        row, _ = self.resolve('7700', 'отчёт для бизнеса shop, выручка 1000000')
        self.assertEqual(row['tenant']['id'], 'cafe')
        row, outcome = self.resolve('7700', 'ctx:shop')  # forged context button for a business they don't belong to
        self.assertEqual((row, outcome), (None, 'handled'))
        self.store.set_context.assert_not_called()
        self.assertIn('недоступен', self.send.call_args.args[3])

    def test_unknown_sender_creates_a_request_and_pings_the_operator(self):
        self.store.memberships.return_value = []
        self.store.list_tenants.return_value = [{'id': 'cafe', 'name': 'Кафе'}]
        self.assertEqual(self.resolve('stranger', 'Кафе Ромашка, кассир'), (None, 'handled'))
        app_module.access_store.submit.assert_called_once_with('whatsapp', 'PN', 'stranger', 'Кафе Ромашка, кассир')
        recipients = [c.args[2] for c in self.send.call_args_list]
        self.assertEqual(recipients, ['stranger', 'op1'])
        self.assertIn('одобрить 9', self.send.call_args_list[1].args[3])
        self.assertIn('cafe — Кафе', self.send.call_args_list[1].args[3])

    def test_multi_membership_without_choice_asks_and_does_not_process(self):
        self.store.memberships.return_value = [self.member('cafe'), self.member('shop')]
        self.assertEqual(self.resolve('7700'), (None, 'handled'))
        self.buttons.assert_called_once()
        self.assertEqual([b[0] for b in self.buttons.call_args.args[4]], ['ctx:cafe', 'ctx:shop'])
        self.assertIn('ещё раз', self.send.call_args.args[3])
        self.store.shared_row.assert_not_called()

    def test_choice_button_sets_context_then_messages_route_there(self):
        self.store.memberships.return_value = [self.member('cafe'), self.member('shop')]
        self.assertEqual(self.resolve('7700', 'ctx:shop'), (None, 'handled'))
        self.store.set_context.assert_called_with('PN', '7700', 'shop')
        self.store.get_context.return_value = 'shop'
        row, _ = self.resolve('7700')
        self.assertEqual(row['tenant']['id'], 'shop')

    def test_expired_or_foreign_context_is_ignored(self):
        self.store.memberships.return_value = [self.member('cafe'), self.member('shop')]
        self.store.get_context.return_value = 'other-business-they-left'
        self.assertEqual(self.resolve('7700'), (None, 'handled'))
        self.store.shared_row.assert_not_called()

    def test_numbered_switch_and_switch_prompt(self):
        self.store.memberships.return_value = [self.member('cafe', 'Кафе'), self.member('shop', 'Магазин')]
        self.assertEqual(self.resolve('7700', 'бизнес 2'), (None, 'handled'))
        self.store.set_context.assert_called_with('PN', '7700', 'shop')
        self.buttons.reset_mock()
        self.resolve('7700', 'бизнес')
        self.buttons.assert_called_once()

    def test_paused_business_is_not_served(self):
        self.store.memberships.return_value = [self.member('cafe')]
        self.store.shared_row.side_effect = lambda *a: None
        self.assertEqual(self.resolve('7700'), (None, 'handled'))
        self.assertIn('недоступен', self.send.call_args.args[3])


class OperatorCommandTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock()
        self.store.shared_number.return_value = {'phone_number_id': 'PN', 'secret_ref': 'PN'}
        self.store.get_tenant.return_value = {'id': 'cafe', 'name': 'Кафе', 'vertical_pack': 'monitor'}
        self.store.list_tenants.return_value = [{'id': 'cafe', 'name': 'Кафе', 'vertical_pack': 'monitor'}]
        self.access = Mock()
        self.access.list_pending.return_value = [{'id': 4, 'sender_id': '7700', 'message': 'Кафе, кассир'}]
        self.access.get.return_value = {'id': 4, 'status': 'pending', 'channel': 'whatsapp', 'routing_key': 'PN', 'sender_id': '7700'}
        self.orig = (app_module.tenant_store, app_module.access_store)
        app_module.tenant_store, app_module.access_store = self.store, self.access
        self.send = patch.object(app_module, 'whatsapp_send_text').start()
        patch.dict(os.environ, {'OPERATOR_WHATSAPP_IDS': 'op1'}).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(lambda: (setattr(app_module, 'tenant_store', self.orig[0]), setattr(app_module, 'access_store', self.orig[1])))
        self.config = {'access_token': 'tok', 'phone_number_id': 'PN'}

    def run_command(self, text, sender='op1'):
        from notimate.operator import handle_operator_command
        return handle_operator_command('PN', self.config, sender, text)

    def test_lists(self):
        self.assertTrue(self.run_command('заявки'))
        self.assertIn('#4 +7700', self.send.call_args.args[3])
        self.assertTrue(self.run_command('клиенты'))
        self.assertIn('cafe — Кафе', self.send.call_args.args[3])

    def test_approve_adds_member_tells_requester_and_marks_request(self):
        self.assertTrue(self.run_command('одобрить 4 cafe кассир Аня'.replace('кассир', 'сотрудник')))
        self.store.add_member.assert_called_once_with('cafe', 'PN', '7700', 'staff', 'Аня', None)
        self.access.decide.assert_called_once_with(4, 'approved', 'cafe', 'staff')
        self.assertEqual([c.args[2] for c in self.send.call_args_list], ['7700', 'op1'])
        self.assertIn('Доступ открыт', self.send.call_args_list[0].args[3])

    def test_approve_owner_and_location(self):
        self.run_command('одобрить 4 cafe владелец')
        self.assertEqual(self.store.add_member.call_args.args[3], 'owner')

    def test_bad_arguments_and_unknown_request(self):
        self.run_command('одобрить 4')
        self.assertIn('Формат', self.send.call_args.args[3])
        self.access.get.return_value = None
        self.run_command('одобрить 99 cafe')
        self.assertIn('не найдена', self.send.call_args.args[3])
        self.store.add_member.assert_not_called()

    def test_reject(self):
        self.run_command('отклонить 4')
        self.access.decide.assert_called_once_with(4, 'rejected')

    def test_create_business_with_defaults_and_next_steps(self):
        self.store.list_tenants.return_value = []
        self.run_command('создать erzhan-cafe KZ monitor Кафе Ержана')
        saved = self.store.upsert_tenant.call_args.args[0]
        self.assertEqual((saved['id'], saved['country'], saved['timezone'], saved['vertical_pack'], saved['name']), ('erzhan-cafe', 'KZ', 'Asia/Almaty', 'monitor', 'Кафе Ержана'))
        self.assertIn('таблица erzhan-cafe', self.send.call_args.args[3])

    def test_create_rejects_bad_input_and_duplicates(self):
        self.store.list_tenants.return_value = [{'id': 'cafe', 'name': 'Кафе'}]
        for bad in ('создать', 'создать Bad_ID KZ monitor X', 'создать erzhan-cafe XX monitor X', 'создать erzhan-cafe KZ nonsense X'):
            self.run_command(bad)
            self.assertIn('Формат', self.send.call_args.args[3])
        self.run_command('создать cafe KZ monitor Ещё')
        self.assertIn('уже есть', self.send.call_args.args[3])
        self.store.upsert_tenant.assert_not_called()

    def test_attach_sheet_creates_tabs_and_saves_id(self):
        self.store.get_tenant.return_value = {'id': 'cafe', 'country': 'KZ', 'modules': {}}
        sheet = '1mf7Ut2llTOfEOUUx1gZGIfTs8j6hoNvibmoB3sLhjGw'
        with patch('notimate.projections.sheet_tabs.init_tabs', return_value=['Закупки', 'Расходы']) as init:
            self.run_command(f'таблица cafe https://docs.google.com/spreadsheets/d/{sheet}/edit?gid=0')
        self.assertEqual((init.call_args.args[1], init.call_args.args[2]), (sheet, 'KZT'))
        self.store.patch_tenant.assert_called_once_with('cafe', sheet_id=sheet)
        self.assertIn('Создано вкладок: 2', self.send.call_args.args[3])

    def test_attach_sheet_without_access_explains_what_to_do(self):
        self.store.get_tenant.return_value = {'id': 'cafe', 'country': 'KZ', 'modules': {}}
        with patch('notimate.projections.sheet_tabs.init_tabs', side_effect=RuntimeError('403')):
            self.run_command('таблица cafe 1mf7Ut2llTOfEOUUx1gZGIfTs8j6hoNvibmoB3sLhjGw')
        self.store.patch_tenant.assert_not_called()
        self.assertIn('Редактор', self.send.call_args.args[3])
        self.run_command('таблица cafe not-a-link')
        self.assertIn('Не вижу ссылку', self.send.call_args.args[3])

    def test_group_and_mode_commands(self):
        self.run_command('группа cafe erzhan')
        self.store.patch_tenant.assert_called_with('cafe', modules_patch={'group': 'erzhan'})
        self.run_command('режим cafe monitor +location_reports')
        self.store.patch_tenant.assert_called_with('cafe', vertical_pack='monitor', modules_patch={'extra_packs': ['location_reports']})
        self.run_command('режим cafe nonsense')
        self.assertIn('Режимы', self.send.call_args.args[3])
        self.run_command('группа ghost erzhan')
        self.assertIn('не найден', self.send.call_args.args[3])

    def test_sheet_url_parsing(self):
        from notimate.projections.sheet_tabs import parse_sheet_id
        sid = '1mf7Ut2llTOfEOUUx1gZGIfTs8j6hoNvibmoB3sLhjGw'
        self.assertEqual(parse_sheet_id(f'https://docs.google.com/spreadsheets/d/{sid}/edit#gid=0'), sid)
        self.assertEqual(parse_sheet_id(sid), sid)
        self.assertIsNone(parse_sheet_id('hello'))

    def test_non_operator_text_is_not_a_command(self):
        from notimate.operator import is_operator
        self.assertFalse(is_operator('7700'))
        self.assertFalse(self.run_command('привет'))
        self.assertFalse(self.run_command('молоко 200'))


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
