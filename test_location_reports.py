"""notimate/packs/location_reports.py: draft → confirm/cancel dispatch for «Отчёты точек»
(Этап 6, docs/21). Postgres itself is mocked here — schema/idempotency are verified
separately against a real local Postgres; this file is about the dispatch logic:
routing button replies, owner commands, and staff reports correctly.
"""

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

from notimate.packs.location_reports import (  # noqa: E402
    DraftAlreadyFinalized,
    format_draft_message,
    format_missing_report,
    format_summary,
    process_location_report_event,
)

ROW = {
    'tenant': {'id': 'erzhan-3biz', 'vertical_pack': 'location_reports', 'name': 'Ержан'},
    'channel': {'channel': 'whatsapp', 'external_id': 'PNID-1', 'secret_ref': 'PNID-1', 'owner_ids': ['77009990001']},
}
CONFIG = {'access_token': 'tok-1', 'phone_number_id': 'PNID-1'}


def inbound(sender_id, text, role='staff'):
    return SimpleNamespace(sender_id=sender_id, text=text, sender_role=role, tenant_id='erzhan-3biz', channel='whatsapp')


class PureFormattingTests(unittest.TestCase):
    def test_draft_message_lists_provided_fields_only(self):
        msg = format_draft_message('Точка 1', {'revenue': 50000, 'cash': 20000, 'non_cash': None, 'external_payouts': None, 'cash_balance': 5000, 'comment': ''})
        self.assertIn('Точка 1', msg)
        self.assertIn('Выручка: 50000', msg)
        self.assertNotIn('Безнал', msg)

    def test_draft_message_includes_comment_when_present(self):
        msg = format_draft_message('Точка 1', {'revenue': 1, 'comment': 'не хватило сдачи'})
        self.assertIn('не хватило сдачи', msg)

    def test_summary_lists_missing_locations(self):
        reports = [{'location_id': 'loc-1', 'location_name': 'Точка 1', 'revenue': 1000, 'cash_balance': 500}]
        locations = [{'id': 'loc-1', 'name': 'Точка 1'}, {'id': 'loc-2', 'name': 'Точка 2'}]
        msg = format_summary(reports, locations, '2026-09-23')
        self.assertIn('1/2', msg)
        self.assertIn('Точка 2', msg)

    def test_summary_with_no_reports(self):
        msg = format_summary([], [{'id': 'loc-1', 'name': 'Точка 1'}], '2026-09-23')
        self.assertIn('отчётов пока нет', msg)

    def test_missing_report_all_reported(self):
        locations = [{'id': 'loc-1', 'name': 'Точка 1'}]
        self.assertIn('Все точки', format_missing_report(locations, {'loc-1'}))

    def test_missing_report_some_missing(self):
        locations = [{'id': 'loc-1', 'name': 'Точка 1'}, {'id': 'loc-2', 'name': 'Точка 2'}]
        msg = format_missing_report(locations, {'loc-1'})
        self.assertIn('Точка 2', msg)
        self.assertNotIn('Точка 1', msg.split(':')[1])


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.original_store = app_module.location_reports_store
        self.original_enabled = app_module.LOCATION_REPORTS_DB_ENABLED
        self.addCleanup(setattr, app_module, 'location_reports_store', self.original_store)
        self.addCleanup(setattr, app_module, 'LOCATION_REPORTS_DB_ENABLED', self.original_enabled)
        self.store = Mock()
        self.store.get_draft.return_value = None  # ownership guard: unknown draft falls through to the KeyError path
        app_module.location_reports_store = self.store
        app_module.LOCATION_REPORTS_DB_ENABLED = True
        self.send_text = patch.object(app_module, 'whatsapp_send_text').start()
        self.send_buttons = patch.object(app_module, 'whatsapp_send_interactive_buttons').start()
        self.addCleanup(patch.stopall)

    def test_button_of_another_sender_or_tenant_is_refused(self):
        for draft in ({'tenant_id': 'erzhan-3biz', 'sender_id': 'someone-else'}, {'tenant_id': 'other-tenant', 'sender_id': '77001112233'}):
            self.store.get_draft.return_value = draft
            for button in ('report:confirm:abc', 'report:cancel:abc', 'report:edit:abc'):
                self.send_text.reset_mock()
                process_location_report_event(ROW, CONFIG, inbound('77001112233', button))
                self.assertIn('не ваш', self.send_text.call_args.args[3])
        self.store.confirm_draft.assert_not_called()
        self.store.cancel_draft.assert_not_called()

    def test_button_of_the_owner_of_the_draft_passes_the_guard(self):
        self.store.get_draft.return_value = {'tenant_id': 'erzhan-3biz', 'sender_id': '77001112233'}
        self.store.confirm_draft.return_value = {'sender_id': '77001112233', 'location_id': 'loc-1'}
        self.store.find_staff.return_value = {'location_name': 'Точка 1'}
        process_location_report_event(ROW, CONFIG, inbound('77001112233', 'report:confirm:abc'))
        self.store.confirm_draft.assert_called_once_with('abc')

    def test_module_disabled_replies_unavailable(self):
        app_module.LOCATION_REPORTS_DB_ENABLED = False
        process_location_report_event(ROW, CONFIG, inbound('77001112233', 'привет'))
        self.send_text.assert_called_once()
        self.assertIn('недоступен', self.send_text.call_args.args[3])

    def test_unregistered_sender_is_told_to_contact_owner(self):
        self.store.find_staff.return_value = None
        process_location_report_event(ROW, CONFIG, inbound('77001112233', 'выручка 50000 нал 20000'))
        self.send_text.assert_called_once()
        self.assertIn('владельцу', self.send_text.call_args.args[3])

    def test_staff_report_creates_draft_and_sends_buttons(self):
        self.store.find_staff.return_value = {'location_id': 'loc-1', 'location_name': 'Точка 1'}
        self.store.create_draft.return_value = 'abc123'
        with patch('notimate.packs.location_reports.analyze_report_text', return_value={'revenue': 50000, 'cash': 20000, 'non_cash': None, 'external_payouts': None, 'cash_balance': 5000, 'comment': ''}):
            process_location_report_event(ROW, CONFIG, inbound('77001112233', 'выручка 50000 нал 20000 остаток 5000'))
        self.store.create_draft.assert_called_once()
        self.send_buttons.assert_called_once()
        buttons = self.send_buttons.call_args.args[4]
        self.assertEqual(len(buttons), 3)
        self.assertEqual(buttons[0][0], 'report:confirm:abc123')
        self.assertEqual(buttons[1][0], 'report:edit:abc123')
        self.assertEqual(buttons[2][0], 'report:cancel:abc123')

    def test_edit_button_cancels_draft_and_asks_to_resend(self):
        self.store.cancel_draft.return_value = {'sender_id': '77001112233', 'location_id': 'loc-1'}
        process_location_report_event(ROW, CONFIG, inbound('77001112233', 'report:edit:abc123'))
        self.store.cancel_draft.assert_called_once_with('abc123')
        self.assertIn('исправленный', self.send_text.call_args.args[3])

    def test_edit_button_on_already_settled_draft_still_asks_to_resend(self):
        # The draft might already be confirmed/cancelled (race with another tap) — editing
        # should still work: we only need the user to resend, not that specific draft.
        self.store.cancel_draft.side_effect = DraftAlreadyFinalized('already done')
        process_location_report_event(ROW, CONFIG, inbound('77001112233', 'report:edit:abc123'))
        self.assertIn('исправленный', self.send_text.call_args.args[3])

    def test_unparseable_text_asks_to_retry(self):
        self.store.find_staff.return_value = {'location_id': 'loc-1', 'location_name': 'Точка 1'}
        with patch('notimate.packs.location_reports.analyze_report_text', return_value=None):
            process_location_report_event(ROW, CONFIG, inbound('77001112233', 'привет как дела'))
        self.store.create_draft.assert_not_called()
        self.send_text.assert_called_once()

    def test_confirm_button_settles_draft_and_confirms(self):
        self.store.confirm_draft.return_value = {'sender_id': '77001112233', 'location_id': 'loc-1'}
        self.store.find_staff.return_value = {'location_name': 'Точка 1'}
        process_location_report_event(ROW, CONFIG, inbound('77001112233', 'report:confirm:abc123'))
        self.store.confirm_draft.assert_called_once_with('abc123')
        self.assertIn('Точка 1', self.send_text.call_args.args[3])

    def test_cancel_button_settles_draft(self):
        self.store.cancel_draft.return_value = {'sender_id': '77001112233', 'location_id': 'loc-1'}
        process_location_report_event(ROW, CONFIG, inbound('77001112233', 'report:cancel:abc123'))
        self.store.cancel_draft.assert_called_once_with('abc123')
        self.assertIn('отменён', self.send_text.call_args.args[3])

    def test_confirm_missing_draft_tells_user_to_resend(self):
        self.store.confirm_draft.side_effect = KeyError('not found')
        process_location_report_event(ROW, CONFIG, inbound('77001112233', 'report:confirm:gone'))
        self.assertIn('не найден', self.send_text.call_args.args[3])

    def test_double_confirm_is_rejected_not_duplicated(self):
        self.store.confirm_draft.side_effect = DraftAlreadyFinalized('already done')
        process_location_report_event(ROW, CONFIG, inbound('77001112233', 'report:confirm:abc123'))
        self.assertIn('уже обработан', self.send_text.call_args.args[3])

    def test_owner_summary_command(self):
        self.store.reports_for_date.return_value = []
        self.store.list_locations.return_value = [{'id': 'loc-1', 'name': 'Точка 1'}]
        process_location_report_event(ROW, CONFIG, inbound('77009990001', 'сводка', role='owner'))
        self.store.reports_for_date.assert_called_once()
        self.send_text.assert_called_once()

    def test_owner_missing_command(self):
        self.store.list_locations.return_value = [{'id': 'loc-1', 'name': 'Точка 1'}]
        self.store.reported_location_ids.return_value = set()
        process_location_report_event(ROW, CONFIG, inbound('77009990001', 'кто не отчитался', role='owner'))
        self.store.reported_location_ids.assert_called_once()

    def test_owner_slash_summary_command_from_the_picker(self):
        # WhatsApp's "/" picker just inserts the command text into the message box — it
        # arrives here as plain text "/summary", not a distinct command payload.
        self.store.reports_for_date.return_value = []
        self.store.list_locations.return_value = [{'id': 'loc-1', 'name': 'Точка 1'}]
        process_location_report_event(ROW, CONFIG, inbound('77009990001', '/summary', role='owner'))
        self.store.reports_for_date.assert_called_once()

    def test_owner_slash_missing_command_from_the_picker(self):
        self.store.list_locations.return_value = [{'id': 'loc-1', 'name': 'Точка 1'}]
        self.store.reported_location_ids.return_value = set()
        process_location_report_event(ROW, CONFIG, inbound('77009990001', '/missing', role='owner'))
        self.store.reported_location_ids.assert_called_once()

    def test_owner_slash_help_command(self):
        process_location_report_event(ROW, CONFIG, inbound('77009990001', '/help', role='owner'))
        self.assertIn('NotiMate', self.send_text.call_args.args[3])
        self.store.reports_for_date.assert_not_called()

    def test_owner_bare_help_command(self):
        process_location_report_event(ROW, CONFIG, inbound('77009990001', 'помощь', role='owner'))
        self.assertIn('NotiMate', self.send_text.call_args.args[3])

    def test_staff_sender_cannot_use_owner_commands(self):
        # A staff member typing "сводка" is not the owner; falls through to report parsing,
        # which then correctly fails to parse it as a report rather than leaking the summary.
        self.store.find_staff.return_value = {'location_id': 'loc-1', 'location_name': 'Точка 1'}
        with patch('notimate.packs.location_reports.analyze_report_text', return_value=None):
            process_location_report_event(ROW, CONFIG, inbound('77001112233', 'сводка', role='staff'))
        self.store.reports_for_date.assert_not_called()


if __name__ == '__main__':
    unittest.main()
