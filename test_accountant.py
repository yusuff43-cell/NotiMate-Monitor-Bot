"""«Бухгалтер» (Этап 7): rules/checks, extraction normalisation, storage, package, intake and
WhatsApp dispatch. Postgres is mocked here; the numbering/idempotency SQL is exercised by
test_accountant_postgres.py when TEST_DATABASE_URL is set."""

import csv
import importlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
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

from notimate.packs.accountant import checks, extract, flow, package, storage  # noqa: E402
from notimate.packs.accountant.rules import rules_for  # noqa: E402
from notimate.packs.accountant.store import DocumentAlreadyFinalized  # noqa: E402

TH = rules_for('TH')
KZ = rules_for('KZ')


def doc(number, doc_type='tax_invoice', seller='Makro', total=1000, date='2026-09-10', **extra):
    return {'doc_number': number, 'doc_type': doc_type, 'seller': seller, 'total': total, 'doc_date': date,
            'subtotal': None, 'vat': None, 'tax_id': '', 'doc_ref': '', 'image_sha256': None, 'storage_ref': None, **extra}


def op(op_id, amount, date='2026-09-10', kind='expense', who='Makro'):
    return {'id': op_id, 'operation_type': kind, 'amount': amount, 'occurred_on': date, 'counterparty': who, 'description': ''}


class RulesTests(unittest.TestCase):
    def test_country_defaults_and_tenant_override(self):
        self.assertTrue(TH['vat_registered'])
        self.assertFalse(KZ['vat_registered'])
        merged = rules_for('KZ', {'accountant': {'rules': {'formal_doc_min_total': 100000, 'bogus': 1}}})
        self.assertEqual(merged['formal_doc_min_total'], 100000)
        self.assertNotIn('bogus', merged)

    def test_unknown_country_falls_back_to_thailand(self):
        self.assertEqual(rules_for('ZZ')['currency'], 'THB')


class ChecksTests(unittest.TestCase):
    def kinds(self, findings):
        return sorted(f['kind'] for f in findings)

    def test_simplified_receipt_needs_full_invoice_in_thailand(self):
        findings = checks.check_month([doc('2026-09-001', 'receipt_simplified')], [], TH, compare_operations=False)
        self.assertEqual(self.kinds(findings), ['missing_full_tax_invoice'])
        self.assertIn('полный tax invoice от Makro', findings[0]['text'])

    def test_simplified_receipt_is_fine_in_kazakhstan_by_default(self):
        self.assertEqual(checks.check_month([doc('2026-09-001', 'receipt_simplified')], [], KZ, compare_operations=False), [])

    def test_formal_document_threshold_when_configured(self):
        rules = rules_for('KZ', {'accountant': {'rules': {'formal_doc_min_total': 500}}})
        findings = checks.check_month([doc('2026-09-001', 'receipt_simplified', total=900)], [], rules, compare_operations=False)
        self.assertEqual(self.kinds(findings), ['missing_formal_document'])

    def test_expense_without_document_and_document_without_expense(self):
        findings = checks.check_month([doc('2026-09-001', total=500)], [op(1, 3450)], TH, compare_operations=True)
        self.assertEqual(self.kinds(findings), ['document_without_expense', 'expense_without_document'])

    def test_matching_document_and_expense_is_clean(self):
        self.assertEqual(checks.check_month([doc('2026-09-001', total=3450)], [op(1, 3450, '2026-09-12')], TH), [])

    def test_revenue_and_salary_never_need_documents(self):
        self.assertEqual(checks.check_month([], [op(1, 9000, kind='revenue'), op(2, 500, kind='salary')], TH), [])

    def test_link_is_one_to_one(self):
        linked, unmatched_docs, unmatched_ops = checks.link_documents_to_operations(
            [doc('a', total=100), doc('b', total=100)], [op(1, 100)], TH)
        self.assertEqual(len(linked), 1)
        self.assertEqual(len(unmatched_docs), 1)
        self.assertEqual(unmatched_ops, [])

    def test_date_window_blocks_far_matches(self):
        linked, _, unmatched_ops = checks.link_documents_to_operations([doc('a', total=100, date='2026-09-01')], [op(1, 100, '2026-09-20')], TH)
        self.assertEqual(linked, [])
        self.assertEqual(len(unmatched_ops), 1)

    def test_duplicate_by_image_hash_and_by_ref(self):
        docs = [doc('2026-09-001', image_sha256='h1'), doc('2026-09-002', image_sha256='h1'),
                doc('2026-09-003', tax_id='0105', doc_ref='INV-9', total=70), doc('2026-09-004', tax_id='0105', doc_ref='INV-9', total=70)]
        dupes = checks.find_duplicates(docs)
        self.assertEqual([d['doc_number'] for d in dupes], ['2026-09-002', '2026-09-004'])

    def test_bank_slip_without_invoice_flagged_and_cleared_by_matching_invoice(self):
        slip = doc('2026-09-001', 'bank_slip', seller='ABC Co', total=5000)
        self.assertEqual(self.kinds(checks.check_month([slip], [], TH, compare_operations=False)), ['transfer_without_invoice'])
        invoice = doc('2026-09-002', 'supplier_invoice', seller='ABC Co', total=5000)
        self.assertEqual(checks.check_month([slip, invoice], [], TH, compare_operations=False), [])

    def test_inconsistent_vat_arithmetic(self):
        findings = checks.check_month([doc('2026-09-001', subtotal=1000, vat=70, total=1200)], [], TH, compare_operations=False)
        self.assertEqual(self.kinds(findings), ['amount_inconsistent'])

    def test_missing_total_or_date(self):
        findings = checks.check_month([doc('2026-09-001', total=None)], [], TH, compare_operations=False)
        self.assertEqual(self.kinds(findings), ['incomplete_fields'])

    def test_summary_orders_actions_first_and_truncates(self):
        findings = [{'kind': 'x', 'severity': 'info', 'text': 'info-item'}] + [{'kind': 'y', 'severity': 'action', 'text': f'act{i}'} for i in range(3)]
        text = checks.summarize_findings(findings, limit=2)
        self.assertLess(text.index('act0'), text.find('info-item') if 'info-item' in text else 10**6)
        self.assertIn('и ещё 2', text)
        self.assertIn('Всё в порядке', checks.summarize_findings([]))


class ExtractTests(unittest.TestCase):
    def test_normalizes_messy_model_output(self):
        fields = extract.parse_document_result('Вот:\n```json\n{"doc_type":"tax_invoice","seller":"Makro","doc_date":"2026-09-10","total":"3,450.50","vat":"225","subtotal":"3,225.50","confidence":0.9,"payment_method":"card"}\n```')
        self.assertEqual(fields['total'], 3450.5)
        self.assertEqual(fields['doc_type'], 'tax_invoice')
        self.assertTrue(extract.document_is_confident(fields))

    def test_unknown_type_bad_date_and_confidence_are_coerced(self):
        fields = extract.parse_document_result('{"doc_type":"weird","doc_date":"вчера","total":"100","confidence":"high","payment_method":"gold"}')
        self.assertEqual((fields['doc_type'], fields['doc_date'], fields['confidence'], fields['payment_method']), ('other', None, 0.0, 'unknown'))
        self.assertFalse(extract.document_is_confident(fields))

    def test_not_a_document_and_garbage(self):
        self.assertIsNone(extract.parse_document_result('NOT_A_DOCUMENT'))
        self.assertIsNone(extract.parse_document_result('нет json'))
        self.assertIsNone(extract.parse_document_result(None))

    def test_comma_decimal_and_thousands(self):
        self.assertEqual(extract._to_number('1 234,50'), 1234.5)
        self.assertEqual(extract._to_number('1,234'), 1234.0)
        self.assertIsNone(extract._to_number('n/a'))

    def test_low_confidence_or_bad_arithmetic_is_not_confident(self):
        base = {'doc_type': 'tax_invoice', 'doc_date': '2026-09-10', 'total': 100.0, 'subtotal': None, 'vat': None, 'confidence': 0.5}
        self.assertFalse(extract.document_is_confident(base))
        self.assertFalse(extract.document_is_confident({**base, 'confidence': 0.95, 'subtotal': 50.0, 'vat': 5.0}))
        self.assertTrue(extract.document_is_confident({**base, 'confidence': 0.95}))


class StorageTests(unittest.TestCase):
    def test_content_addressed_idempotent_and_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ref1, sha1 = storage.save_original('t/1 ../x', b'abc', 'image/jpeg', root)
            ref2, sha2 = storage.save_original('t/1 ../x', b'abc', 'image/jpeg', root)
            self.assertEqual((ref1, sha1), (ref2, sha2))
            self.assertNotIn('.', ref1.split('/')[0])
            self.assertEqual(storage.read_original(ref1, root), b'abc')
            with self.assertRaises(ValueError):
                storage.read_original('../../etc/passwd', root)


class PackageTests(unittest.TestCase):
    def test_zip_contents_and_csv_injection_guard(self):
        docs = [doc('2026-09-001', seller='=HYPERLINK("x")', total=100, storage_ref='t/aaa.jpg'), doc('2026-09-002', storage_ref='t/bbb.png')]
        blobs = {'t/aaa.jpg': b'JPG', 't/bbb.png': b'PNG'}
        data, summary = package.build_month_package('Клиент', '2026-09', docs, [], [], blobs.__getitem__)
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        self.assertIn('2026-09/реестр.csv', names)
        self.assertIn('2026-09/фото/2026-09-001.jpg', names)
        self.assertIn('2026-09/фото/2026-09-002.png', names)
        self.assertIn('2026-09/список_оригиналов.csv', names)
        registry = zipfile.ZipFile(io.BytesIO(data)).read('2026-09/реестр.csv').decode('utf-8-sig')
        rows = list(csv.reader(io.StringIO(registry), delimiter=';'))
        self.assertEqual(rows[1][3][0], "'")  # neutralised formula
        self.assertIn('Документов: 2', summary)

    def test_missing_original_is_reported_not_fatal(self):
        def read(ref):
            raise FileNotFoundError(ref)
        data, summary = package.build_month_package('К', '2026-09', [doc('2026-09-001', storage_ref='t/x.jpg')], [], [], read)
        questions = zipfile.ZipFile(io.BytesIO(data)).read('2026-09/открытые_вопросы.txt').decode()
        self.assertIn('не найден в хранилище', questions)

    def test_previous_period_handles_january(self):
        import datetime as dt
        self.assertEqual(package.previous_period(dt.date(2027, 1, 1)), '2026-12')
        self.assertEqual(package.previous_period(dt.date(2026, 10, 3)), '2026-09')

    def test_email_is_noop_without_smtp(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('SMTP_HOST', None)
            self.assertFalse(package.send_email(['a@b.c'], 's', 'b', 'f.zip', b'x'))


FIELDS = {'doc_type': 'tax_invoice', 'seller': 'Makro', 'tax_id': '', 'doc_ref': '', 'doc_date': '2026-09-10', 'subtotal': None, 'vat': None,
          'total': 3450.0, 'currency': 'THB', 'payment_method': 'card', 'confidence': 0.9, 'note': ''}
TENANT = {'id': 'aspan', 'name': 'Aspan', 'country': 'KZ', 'timezone': 'Asia/Almaty', 'vertical_pack': 'accountant', 'modules': {'accountant': {'accountant_ids': ['acc1']}}}


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock()
        self.store.find_by_event.return_value = None
        self.store.find_by_hash.return_value = None
        self.store.create_document.return_value = (7, True)
        self.store.confirm_document.return_value = {'id': 7, 'doc_number': '2026-09-001', **FIELDS}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.dict(os.environ, {'DOCUMENT_STORAGE_DIR': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_intake(self, fields=FIELDS, **kw):
        with patch.object(flow, 'analyze_document_image', return_value=fields):
            return flow.intake_document(self.store, TENANT, 's1', 'ev1', b'IMG', 'image/jpeg', fallback_date='2026-09-24', **kw)

    def test_confident_document_is_numbered_immediately(self):
        result = self.run_intake()
        self.assertEqual(result['status'], 'confirmed')
        self.store.confirm_document.assert_called_once()

    def test_low_confidence_becomes_draft(self):
        result = self.run_intake({**FIELDS, 'confidence': 0.3})
        self.assertEqual(result['status'], 'draft')
        self.store.confirm_document.assert_not_called()

    def test_retry_of_same_event_does_not_call_the_model(self):
        self.store.find_by_event.return_value = {'id': 7}
        with patch.object(flow, 'analyze_document_image') as analyze:
            result = flow.intake_document(self.store, TENANT, 's1', 'ev1', b'IMG', 'image/jpeg', fallback_date='2026-09-24')
        self.assertEqual(result['status'], 'exists')
        analyze.assert_not_called()

    def test_same_image_twice_is_a_duplicate_without_model_call(self):
        self.store.find_by_hash.return_value = {'doc_number': '2026-09-004'}
        with patch.object(flow, 'analyze_document_image') as analyze:
            result = flow.intake_document(self.store, TENANT, 's1', 'ev2', b'IMG', 'image/jpeg', fallback_date='2026-09-24')
        self.assertEqual(result['status'], 'duplicate')
        analyze.assert_not_called()

    def test_not_a_document_stores_nothing(self):
        result = self.run_intake(None)
        self.assertEqual(result['status'], 'not_document')
        self.store.create_document.assert_not_called()

    def test_policy_override_forces_auto_for_line(self):
        result = self.run_intake({**FIELDS, 'confidence': 0.1}, policy='auto')
        self.assertEqual(result['status'], 'confirmed')
        self.assertFalse(result['confident'])


def inbound(text='', media=(), role='staff', sender='staff1'):
    return SimpleNamespace(sender_id=sender, text=text, sender_role=role, media=media, tenant_id='aspan', channel='whatsapp', external_event_id='wamid.1')


ROW = {'tenant': TENANT, 'channel': {'channel': 'whatsapp', 'external_id': 'PN', 'secret_ref': 'PN', 'owner_ids': ['owner1']}}
CONFIG = {'access_token': 'tok', 'phone_number_id': 'PN', 'timezone': 'Asia/Almaty'}


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.orig = (app_module.documents_store, app_module.DOCUMENTS_DB_ENABLED)
        self.addCleanup(lambda: (setattr(app_module, 'documents_store', self.orig[0]), setattr(app_module, 'DOCUMENTS_DB_ENABLED', self.orig[1])))
        self.store = Mock()
        app_module.documents_store = self.store
        app_module.DOCUMENTS_DB_ENABLED = True
        self.send_text = patch.object(app_module, 'whatsapp_send_text').start()
        self.send_buttons = patch.object(app_module, 'whatsapp_send_interactive_buttons').start()
        self.send_doc = patch.object(app_module, 'whatsapp_send_document').start()
        self.download = patch.object(app_module, 'whatsapp_download_media', return_value=(b'img', 'image/jpeg')).start()
        self.addCleanup(patch.stopall)

    def last_text(self):
        return self.send_text.call_args.args[3]

    def photo(self, **kw):
        return inbound(media=({'kind': 'image', 'id': 'M1', 'mime_type': 'image/jpeg'},), **kw)

    def test_module_disabled(self):
        app_module.DOCUMENTS_DB_ENABLED = False
        flow.process_accountant_event(ROW, CONFIG, inbound('привет'))
        self.assertIn('недоступен', self.last_text())

    def test_confirmed_photo_replies_with_number(self):
        with patch.object(flow, 'intake_document', return_value={'status': 'confirmed', 'doc': {'doc_number': '2026-09-017', **FIELDS}}):
            flow.process_accountant_event(ROW, CONFIG, self.photo())
        self.assertIn('2026-09-017', self.last_text())
        self.assertIn('оригинале', self.last_text())

    def test_draft_photo_sends_three_buttons(self):
        with patch.object(flow, 'intake_document', return_value={'status': 'draft', 'doc_id': 9, 'fields': FIELDS}):
            flow.process_accountant_event(ROW, CONFIG, self.photo())
        buttons = self.send_buttons.call_args.args[4]
        self.assertEqual([b[0] for b in buttons], ['doc:confirm:9', 'doc:edit:9', 'doc:cancel:9'])

    def test_retry_replies_nothing(self):
        with patch.object(flow, 'intake_document', return_value={'status': 'exists', 'doc': {}}):
            flow.process_accountant_event(ROW, CONFIG, self.photo())
        self.send_text.assert_not_called()

    def test_duplicate_and_not_document_messages(self):
        with patch.object(flow, 'intake_document', return_value={'status': 'duplicate', 'doc': {'doc_number': '2026-09-004'}}):
            flow.process_accountant_event(ROW, CONFIG, self.photo())
        self.assertIn('2026-09-004', self.last_text())
        with patch.object(flow, 'intake_document', return_value={'status': 'not_document'}):
            flow.process_accountant_event(ROW, CONFIG, self.photo())
        self.assertIn('Не вижу', self.last_text())

    def test_media_download_failure(self):
        self.download.side_effect = RuntimeError('x')
        flow.process_accountant_event(ROW, CONFIG, self.photo())
        self.assertIn('фото', self.last_text())

    def test_confirm_button_numbers_once_and_double_tap_is_rejected(self):
        self.store.confirm_document.return_value = {'doc_number': '2026-09-001', **FIELDS}
        flow.process_accountant_event(ROW, CONFIG, inbound('doc:confirm:9'))
        self.assertIn('2026-09-001', self.last_text())
        self.store.confirm_document.side_effect = DocumentAlreadyFinalized('again')
        flow.process_accountant_event(ROW, CONFIG, inbound('doc:confirm:9'))
        self.assertIn('уже обработан', self.last_text())

    def test_cancel_and_edit_buttons_reject_the_draft(self):
        flow.process_accountant_event(ROW, CONFIG, inbound('doc:cancel:9'))
        self.store.reject_document.assert_called_with(9)
        self.assertIn('отменён', self.last_text())
        flow.process_accountant_event(ROW, CONFIG, inbound('doc:edit:9'))
        self.assertIn('ещё раз', self.last_text())

    def test_owner_package_command_sends_zip_to_owner(self):
        self.store.list_confirmed.return_value = [doc('2026-09-001', storage_ref=None)]
        self.store.list_operations.return_value = []
        self.store.open_questions.return_value = []
        flow.process_accountant_event(ROW, CONFIG, inbound('пакет 2026-09', role='owner', sender='owner1'))
        self.send_doc.assert_called_once()
        self.assertEqual(self.send_doc.call_args.args[2], 'owner1')
        self.assertEqual(self.send_doc.call_args.args[3], 'aspan-2026-09.zip')
        self.store.mark_period.assert_called_with('aspan', '2026-09', 'sent')

    def test_staff_cannot_request_package(self):
        flow.process_accountant_event(ROW, CONFIG, inbound('пакет'))
        self.send_doc.assert_not_called()

    def test_owner_missing_command(self):
        self.store.list_confirmed.return_value = [doc('2026-09-001', 'receipt_simplified')]
        self.store.list_operations.return_value = []
        flow.process_accountant_event({**ROW, 'tenant': {**TENANT, 'country': 'TH'}}, {**CONFIG}, inbound('/missing', role='owner', sender='owner1'))
        self.assertIn('полный tax invoice', self.last_text())

    def test_accountant_question_is_stored_and_forwarded(self):
        flow.process_accountant_event(ROW, CONFIG, inbound('вопрос 2026-09-017 нужен tax invoice', sender='acc1'))
        args = self.store.add_question.call_args.args
        self.assertEqual(args[3], '2026-09')
        self.assertEqual(args[4], '2026-09-017')
        self.assertEqual(self.send_text.call_args_list[-1].args[2], 'owner1')

    def test_accountant_accept_marks_period(self):
        flow.process_accountant_event(ROW, CONFIG, inbound('принято 2026-09', sender='acc1'))
        self.store.mark_period.assert_called_with('aspan', '2026-09', 'accepted')

    def test_help_and_unknown_text(self):
        flow.process_accountant_event(ROW, CONFIG, inbound('помощь'))
        self.assertIn('документы для бухгалтера', self.last_text())
        flow.process_accountant_event(ROW, CONFIG, inbound('привет'))
        self.assertIn('Пришлите фото', self.last_text())


class LineHookTests(unittest.TestCase):
    def setUp(self):
        self.orig = (app_module.documents_store, app_module.DOCUMENTS_DB_ENABLED)
        self.addCleanup(lambda: (setattr(app_module, 'documents_store', self.orig[0]), setattr(app_module, 'DOCUMENTS_DB_ENABLED', self.orig[1])))
        app_module.documents_store = Mock()
        app_module.DOCUMENTS_DB_ENABLED = True
        self.addCleanup(patch.stopall)
        self.event = {'webhookEventId': 'e1', 'source': {'type': 'group', 'groupId': 'G1', 'userId': 'U1'}}

    def test_noop_without_flag(self):
        with patch.object(flow, 'intake_document') as intake:
            flow.register_line_document('dest', {'name': 'JSC'}, self.event, b'x')
        intake.assert_not_called()

    def test_enabled_files_document_and_replies_in_group(self):
        cfg = {'name': 'JSC', 'channel_access_token': 'x', 'modules': {'accountant': {'enabled': True}}}
        api = Mock()
        patch.object(app_module, 'get_line_api', return_value=api).start()
        with patch.object(flow, 'intake_document', return_value={'status': 'confirmed', 'confident': True, 'doc': {'doc_number': '2026-09-001', **FIELDS}}) as intake:
            flow.register_line_document('dest', cfg, self.event, b'x')
        self.assertEqual(intake.call_args.kwargs['policy'], 'auto')
        self.assertEqual(api.push_message.call_args.args[0].to, 'G1')

    def test_failures_never_propagate(self):
        cfg = {'modules': {'accountant': {'enabled': True}}}
        with patch.object(flow, 'intake_document', side_effect=RuntimeError('db down')):
            flow.register_line_document('dest', cfg, self.event, b'x')  # must not raise


if __name__ == '__main__':
    unittest.main()
