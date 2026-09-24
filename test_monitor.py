"""«Монитор» для WhatsApp: доступ по номерам, текст/фото/PDF → общие обработчики,
команды владельца, регистрация документа, валюта и часовой пояс tenant."""

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

from notimate import pipeline  # noqa: E402
from notimate.packs import monitor  # noqa: E402
from notimate.inbound import build_whatsapp_inbound_message  # noqa: E402

ROW = {
    'tenant': {'id': 'erzhan', 'name': 'Ержан', 'country': 'KZ', 'timezone': 'Asia/Almaty', 'vertical_pack': 'monitor',
               'sheet_id': 'SHEET1', 'business_type': 'retail', 'modules': {}},
    'channel': {'channel': 'whatsapp', 'external_id': 'PN', 'secret_ref': 'PN', 'owner_ids': ['owner1'], 'allowed_chats': ['staff1']},
}
CONFIG = {'access_token': 'tok', 'phone_number_id': 'PN', 'timezone': 'Asia/Almaty', 'owner_ids': ['owner1']}


def inbound(sender, text='', media=(), role=None):
    return SimpleNamespace(sender_id=sender, text=text, media=media, sender_role=role or ('owner' if sender == 'owner1' else 'staff'),
                           external_event_id='wamid.1', tenant_id='erzhan', channel='whatsapp')


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.orig = (app_module.SHEETS_ENABLED, app_module.documents_store, app_module.DOCUMENTS_DB_ENABLED)
        self.addCleanup(lambda: (setattr(app_module, 'SHEETS_ENABLED', self.orig[0]), setattr(app_module, 'documents_store', self.orig[1]), setattr(app_module, 'DOCUMENTS_DB_ENABLED', self.orig[2])))
        app_module.SHEETS_ENABLED = True
        self.send = patch.object(app_module, 'whatsapp_send_text').start()
        self.download = patch.object(app_module, 'whatsapp_download_media', return_value=(b'img', 'image/jpeg')).start()
        self.handle_text = patch.object(pipeline, 'handle_text', return_value=None).start()
        self.handle_image = patch.object(pipeline, 'handle_image', return_value=None).start()
        self.addCleanup(patch.stopall)

    def last(self):
        return self.send.call_args.args[3]

    def test_unknown_sender_is_refused_and_nothing_processed(self):
        monitor.process_monitor_event(ROW, CONFIG, inbound('stranger', 'купили молоко'))
        self.assertIn('Доступ не настроен', self.last())
        self.handle_text.assert_not_called()

    def test_missing_sheet_is_reported(self):
        row = {**ROW, 'tenant': {**ROW['tenant'], 'sheet_id': None}}
        monitor.process_monitor_event(row, CONFIG, inbound('staff1', 'x'))
        self.assertIn('не подключена', self.last())

    def test_staff_text_goes_to_shared_handler_with_tenant_cfg_and_is_acknowledged(self):
        monitor.process_monitor_event(ROW, CONFIG, inbound('staff1', 'купили молоко 200'))
        args = self.handle_text.call_args.args
        self.assertEqual(args[0], 'erzhan')
        cfg = args[1]
        self.assertEqual((cfg['sheet_id'], cfg['currency'], cfg['timezone'], cfg['glossary']), ('SHEET1', 'KZT', 'Asia/Almaty', 'none'))
        self.assertEqual(args[3], 'купили молоко 200')
        self.assertIn('Принято', self.last())

    def test_ignored_text_gets_no_reply(self):
        self.handle_text.return_value = 'ignored'
        monitor.process_monitor_event(ROW, CONFIG, inbound('staff1', 'привет'))
        self.send.assert_not_called()

    def test_owner_notifications_route_to_whatsapp_owners(self):
        monitor.process_monitor_event(ROW, CONFIG, inbound('staff1', 'x'))
        cfg = self.handle_text.call_args.args[1]
        self.send.reset_mock()
        app_module.notify_owner(cfg, 'РАСХОД записан')
        self.assertEqual([c.args[2] for c in self.send.call_args_list], ['owner1'])

    def test_photo_is_downloaded_and_processed_then_acknowledged(self):
        media = ({'kind': 'image', 'id': 'M1', 'mime_type': 'image/jpeg'},)
        monitor.process_monitor_event(ROW, CONFIG, inbound('staff1', media=media))
        self.download.assert_called_once_with('tok', 'M1')
        self.assertEqual(self.handle_image.call_args.args[3:], (b'img', 'image/jpeg'))
        self.assertIn('Принято', self.last())

    def test_pdf_is_processed_with_pdf_mime(self):
        self.download.return_value = (b'%PDF', 'application/pdf')
        media = ({'kind': 'pdf', 'id': 'M2', 'mime_type': 'application/pdf'},)
        monitor.process_monitor_event(ROW, CONFIG, inbound('staff1', media=media))
        self.assertEqual(self.handle_image.call_args.args[4], 'application/pdf')

    def test_non_finance_photo_asks_for_a_document(self):
        self.handle_image.return_value = 'ignored'
        monitor.process_monitor_event(ROW, CONFIG, inbound('staff1', media=({'kind': 'image', 'id': 'M1'},)))
        self.assertIn('Не вижу', self.last())

    def test_download_failure_asks_to_resend(self):
        self.download.side_effect = RuntimeError('x')
        monitor.process_monitor_event(ROW, CONFIG, inbound('staff1', media=({'kind': 'image', 'id': 'M1'},)))
        self.assertIn('ещё раз', self.last())
        self.handle_image.assert_not_called()

    def test_owner_commands_use_shared_reports(self):
        for word, target in (('деньги', 'evening_summary'), ('/report', 'detailed_report'), ('напоминания', 'reminders_report'), ('неделя', 'weekly_report')):
            with patch.object(app_module, target) as report:
                monitor.process_monitor_event(ROW, CONFIG, inbound('owner1', word))
            report.assert_called_once()
        self.handle_text.assert_not_called()

    def test_staff_cannot_run_owner_commands(self):
        with patch.object(app_module, 'evening_summary') as report:
            monitor.process_monitor_event(ROW, CONFIG, inbound('staff1', 'деньги'))
        report.assert_not_called()

    def test_owner_free_text_is_processed_like_staff_text(self):
        monitor.process_monitor_event(ROW, CONFIG, inbound('owner1', 'купил лёд 300'))
        self.handle_text.assert_called_once()

    def test_dashboard_command_without_feature_says_not_connected(self):
        monitor.process_monitor_event(ROW, CONFIG, inbound('owner1', 'дашборд'))
        self.assertIn('не подключена', self.last())

    def test_accountant_module_files_document_and_returns_number(self):
        row = {**ROW, 'tenant': {**ROW['tenant'], 'modules': {'accountant': {'enabled': True}}}}
        app_module.documents_store = Mock()
        app_module.DOCUMENTS_DB_ENABLED = True
        with patch('notimate.packs.accountant.flow.intake_document', return_value={'status': 'confirmed', 'doc': {'doc_number': '2026-09-004'}}) as intake:
            monitor.process_monitor_event(row, CONFIG, inbound('staff1', media=({'kind': 'image', 'id': 'M1'},)))
        self.assertEqual(intake.call_args.kwargs['policy'], 'auto')
        self.assertIn('2026-09-004', self.last())
        self.handle_image.assert_called_once()

    def test_duplicate_document_is_not_recorded_twice(self):
        row = {**ROW, 'tenant': {**ROW['tenant'], 'modules': {'accountant': {'enabled': True}}}}
        app_module.documents_store = Mock()
        app_module.DOCUMENTS_DB_ENABLED = True
        with patch('notimate.packs.accountant.flow.intake_document', return_value={'status': 'duplicate', 'doc': {'doc_number': '2026-09-001'}}):
            monitor.process_monitor_event(row, CONFIG, inbound('staff1', media=({'kind': 'image', 'id': 'M1'},)))
        self.handle_image.assert_not_called()
        self.assertIn('уже загружен', self.last())

    def test_registry_failure_does_not_block_bookkeeping(self):
        row = {**ROW, 'tenant': {**ROW['tenant'], 'modules': {'accountant': {'enabled': True}}}}
        app_module.documents_store = Mock()
        app_module.DOCUMENTS_DB_ENABLED = True
        with patch('notimate.packs.accountant.flow.intake_document', side_effect=RuntimeError('db')):
            monitor.process_monitor_event(row, CONFIG, inbound('staff1', media=({'kind': 'image', 'id': 'M1'},)))
        self.handle_image.assert_called_once()


class InboundPdfTests(unittest.TestCase):
    def test_pdf_document_becomes_pdf_media_and_other_documents_are_ignored(self):
        msg = {'from': 's', 'id': 'w', 'type': 'document', 'document': {'id': 'D1', 'mime_type': 'application/pdf', 'filename': 'inv.pdf', 'caption': 'Makro'}}
        inbound_msg = build_whatsapp_inbound_message(ROW, msg)
        self.assertEqual((inbound_msg.media[0]['kind'], inbound_msg.text), ('pdf', 'Makro'))
        docx = {**msg, 'document': {'id': 'D2', 'mime_type': 'application/zip'}}
        self.assertEqual(build_whatsapp_inbound_message(ROW, docx).media, ())


class SharedHandlerTests(unittest.TestCase):
    """The refactor must keep the LINE behaviour and add tenant currency/timezone."""

    def setUp(self):
        for name in ('save_закупки', 'save_остатки', 'save_расходы', 'save_выручка', 'save_проблемы', 'record_operation_safely', 'record_stock_signal_safely', 'record_issue_safely', 'refresh_overview_safely', 'notify_owner'):
            patch.object(app_module, name).start()
        self.addCleanup(patch.stopall)

    def test_kzt_tenant_expense_uses_currency_in_ledger_sheet_and_message(self):
        cfg = {'sheet_id': 'S', 'currency': 'KZT', 'timezone': 'Asia/Almaty', 'name': 'K'}
        answer = '{"type":"text_expense","supplier":"Рынок","items":[{"description":"мясо"}],"total":"12000"}'
        with patch.object(app_module, 'analyze_text', return_value=answer):
            self.assertIsNone(pipeline.handle_text('t1', cfg, 'e1', 'мясо 12000'))
        app_module.save_расходы.assert_called_once()
        self.assertEqual(app_module.save_расходы.call_args.kwargs['currency'], 'KZT')
        self.assertEqual(app_module.record_operation_safely.call_args.args[5], 'KZT')
        self.assertIn('12000 KZT', app_module.notify_owner.call_args.args[1])

    def test_thb_tenant_calls_are_unchanged_no_currency_kwarg(self):
        cfg = {'sheet_id': 'S', 'name': 'JSC'}
        answer = '{"type":"text_expense","supplier":"Makro","items":[{"description":"ice"}],"total":"35"}'
        with patch.object(app_module, 'analyze_text', return_value=answer):
            pipeline.handle_text('t1', cfg, 'e1', 'ice 35฿')
        self.assertNotIn('currency', app_module.save_расходы.call_args.kwargs)
        self.assertIn('35 THB', app_module.notify_owner.call_args.args[1])

    def test_ignored_results_are_reported_to_the_caller(self):
        with patch.object(app_module, 'analyze_text', return_value='IGNORE'):
            self.assertEqual(pipeline.handle_text('t1', {'sheet_id': 'S'}, 'e1', 'привет'), 'ignored')
        with patch.object(app_module, 'analyze_image', return_value='NOT_FINANCE'):
            self.assertEqual(pipeline.handle_image('t1', {'sheet_id': 'S'}, 'e1', b'x'), 'ignored')

    def test_image_mime_reaches_the_analyzer(self):
        with patch.object(app_module, 'analyze_image', return_value='NOT_FINANCE') as analyze:
            pipeline.handle_image('t1', {'sheet_id': 'S'}, 'e1', b'x', 'application/pdf')
        self.assertEqual(analyze.call_args.args[2], 'application/pdf')


if __name__ == '__main__':
    unittest.main()
