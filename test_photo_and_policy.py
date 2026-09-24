"""Этап 5 (confirmation policy) and Этап 6 photo reports: media I/O, inbound media,
policy resolution, photo → draft/auto-save routing. Network and OpenAI are mocked."""

import importlib
import io
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

from notimate.channels import whatsapp  # noqa: E402
from notimate.inbound import build_whatsapp_inbound_message  # noqa: E402
from notimate.packs import location_reports as lr  # noqa: E402
from notimate.policy import AUTO, CONFIRM, CONFIRM_IF_LOW_CONFIDENCE, needs_confirmation, resolve_policy  # noqa: E402

ROW = {
    'tenant': {'id': 't1', 'vertical_pack': 'location_reports', 'timezone': 'Asia/Almaty', 'modules': {}},
    'channel': {'channel': 'whatsapp', 'external_id': 'PN', 'secret_ref': 'PN', 'owner_ids': ['owner']},
}
CONFIG = {'access_token': 'tok', 'phone_number_id': 'PN', 'timezone': 'Asia/Almaty'}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class MediaTests(unittest.TestCase):
    def test_download_media_two_step_with_bearer(self):
        calls = []

        def fake_urlopen(request, timeout=0):
            calls.append((request.full_url, request.get_header('Authorization')))
            if request.full_url.endswith('/MEDIA1'):
                return FakeResponse(json.dumps({'url': 'https://cdn.example/x', 'mime_type': 'image/png', 'file_size': 10}).encode())
            return FakeResponse(b'PNGDATA')

        with patch('urllib.request.urlopen', fake_urlopen):
            data, mime = whatsapp.download_media('tok', 'MEDIA1')
        self.assertEqual((data, mime), (b'PNGDATA', 'image/png'))
        self.assertTrue(all(auth == 'Bearer tok' for _, auth in calls))
        self.assertEqual(len(calls), 2)

    def test_download_media_rejects_oversized_before_fetching(self):
        def fake_urlopen(request, timeout=0):
            return FakeResponse(json.dumps({'url': 'https://cdn.example/x', 'file_size': 99_000_000}).encode())

        with patch('urllib.request.urlopen', fake_urlopen):
            with self.assertRaisesRegex(RuntimeError, 'too large'):
                whatsapp.download_media('tok', 'MEDIA1')

    def test_download_media_without_url_fails(self):
        with patch('urllib.request.urlopen', lambda r, timeout=0: FakeResponse(b'{}')):
            with self.assertRaises(RuntimeError):
                whatsapp.download_media('tok', 'MEDIA1')

    def test_send_document_uploads_then_sends_by_media_id(self):
        seen = []

        def fake_urlopen(request, timeout=0):
            seen.append(request)
            if request.full_url.endswith('/media'):
                return FakeResponse(b'{"id": "UP1"}')
            return FakeResponse(b'{"messages": [{"id": "wamid.x"}]}')

        with patch('urllib.request.urlopen', fake_urlopen):
            whatsapp.send_document('tok', 'PN', '7700', 'reg.csv', 'text/csv', b'a,b', 'Реестр')
        self.assertIn('multipart/form-data', seen[0].get_header('Content-type'))
        self.assertIn(b'a,b', seen[0].data)
        body = json.loads(seen[1].data)
        self.assertEqual(body['type'], 'document')
        self.assertEqual(body['document']['id'], 'UP1')
        self.assertEqual(body['document']['filename'], 'reg.csv')


class InboundMediaTests(unittest.TestCase):
    def test_image_message_becomes_media_with_caption_as_text(self):
        msg = {'from': '7700', 'id': 'w1', 'type': 'image', 'image': {'id': 'M1', 'mime_type': 'image/jpeg', 'caption': 'Точка 1'}}
        inbound = build_whatsapp_inbound_message(ROW, msg)
        self.assertEqual(inbound.text, 'Точка 1')
        self.assertEqual(inbound.media[0]['id'], 'M1')
        self.assertEqual(inbound.media[0]['kind'], 'image')

    def test_text_message_has_no_media(self):
        msg = {'from': '7700', 'id': 'w2', 'type': 'text', 'text': {'body': 'привет'}}
        self.assertEqual(build_whatsapp_inbound_message(ROW, msg).media, ())


class PolicyTests(unittest.TestCase):
    def test_defaults_by_pack(self):
        self.assertEqual(resolve_policy({}, 'location_reports', 'report'), CONFIRM)
        self.assertEqual(resolve_policy({}, 'accountant', 'document'), CONFIRM_IF_LOW_CONFIDENCE)
        self.assertEqual(resolve_policy({}, 'unknown_pack', 'anything'), CONFIRM)

    def test_tenant_override(self):
        tenant = {'modules': {'confirmation': {'report': 'auto'}}}
        self.assertEqual(resolve_policy(tenant, 'location_reports', 'report'), AUTO)

    def test_invalid_override_falls_back_to_safe_default(self):
        tenant = {'modules': {'confirmation': {'report': 'yolo'}}}
        self.assertEqual(resolve_policy(tenant, 'location_reports', 'report'), CONFIRM)

    def test_needs_confirmation_matrix(self):
        self.assertFalse(needs_confirmation(AUTO, False))
        self.assertTrue(needs_confirmation(CONFIRM, True))
        self.assertFalse(needs_confirmation(CONFIRM_IF_LOW_CONFIDENCE, True))
        self.assertTrue(needs_confirmation(CONFIRM_IF_LOW_CONFIDENCE, False))


class ConfidenceTests(unittest.TestCase):
    def test_consistent_report_is_confident(self):
        self.assertTrue(lr.report_is_confident({'revenue': 100, 'cash': 60, 'non_cash': 40}))

    def test_missing_revenue_is_not_confident(self):
        self.assertFalse(lr.report_is_confident({'cash': 60}))

    def test_split_mismatch_is_not_confident(self):
        self.assertFalse(lr.report_is_confident({'revenue': 100, 'cash': 10, 'non_cash': 10}))

    def test_negative_or_garbage_is_not_confident(self):
        self.assertFalse(lr.report_is_confident({'revenue': -5}))
        self.assertFalse(lr.report_is_confident({'revenue': 'много'}))


def inbound(text='', media=(), role='staff', sender='staff1'):
    return SimpleNamespace(sender_id=sender, text=text, sender_role=role, media=media, tenant_id='t1', channel='whatsapp')


class PhotoReportDispatchTests(unittest.TestCase):
    def setUp(self):
        self.orig = (app_module.location_reports_store, app_module.LOCATION_REPORTS_DB_ENABLED)
        self.addCleanup(lambda: (setattr(app_module, 'location_reports_store', self.orig[0]),
                                 setattr(app_module, 'LOCATION_REPORTS_DB_ENABLED', self.orig[1])))
        self.store = Mock()
        self.store.find_staff.return_value = {'location_id': 'loc-1', 'location_name': 'Точка 1'}
        self.store.create_draft.return_value = 'd1'
        app_module.location_reports_store = self.store
        app_module.LOCATION_REPORTS_DB_ENABLED = True
        self.send_text = patch.object(app_module, 'whatsapp_send_text').start()
        self.send_buttons = patch.object(app_module, 'whatsapp_send_interactive_buttons').start()
        self.download = patch.object(app_module, 'whatsapp_download_media', return_value=(b'img', 'image/jpeg')).start()
        self.addCleanup(patch.stopall)

    def photo(self):
        return inbound(text='подпись', media=({'kind': 'image', 'id': 'M1', 'mime_type': 'image/jpeg'},))

    def test_photo_report_goes_to_draft_with_buttons(self):
        fields = {'revenue': 100, 'cash': 60, 'non_cash': 40, 'external_payouts': None, 'cash_balance': 10, 'comment': ''}
        with patch.object(lr, 'analyze_report_image', return_value=fields) as analyze:
            lr.process_location_report_event(ROW, CONFIG, self.photo())
        self.download.assert_called_once_with('tok', 'M1')
        self.assertEqual(analyze.call_args.args[2], 'подпись')
        self.send_buttons.assert_called_once()
        self.store.confirm_draft.assert_not_called()

    def test_photo_without_report_data_asks_again(self):
        with patch.object(lr, 'analyze_report_image', return_value=None):
            lr.process_location_report_event(ROW, CONFIG, self.photo())
        self.store.create_draft.assert_not_called()
        self.assertIn('распознать', self.send_text.call_args.args[3])

    def test_media_download_failure_is_reported_not_raised(self):
        self.download.side_effect = RuntimeError('boom')
        lr.process_location_report_event(ROW, CONFIG, self.photo())
        self.assertIn('фото', self.send_text.call_args.args[3])
        self.store.create_draft.assert_not_called()

    def test_auto_policy_saves_confident_report_without_buttons(self):
        row = {**ROW, 'tenant': {**ROW['tenant'], 'modules': {'confirmation': {'report': CONFIRM_IF_LOW_CONFIDENCE}}}}
        fields = {'revenue': 100, 'cash': 60, 'non_cash': 40, 'comment': ''}
        with patch.object(lr, 'analyze_report_text', return_value=fields):
            lr.process_location_report_event(row, CONFIG, inbound(text='выручка 100 нал 60 безнал 40'))
        self.store.confirm_draft.assert_called_once_with('d1')
        self.send_buttons.assert_not_called()
        self.assertIn('сохранён', self.send_text.call_args.args[3])

    def test_low_confidence_falls_back_to_draft(self):
        row = {**ROW, 'tenant': {**ROW['tenant'], 'modules': {'confirmation': {'report': CONFIRM_IF_LOW_CONFIDENCE}}}}
        fields = {'revenue': 100, 'cash': 5, 'non_cash': 5, 'comment': ''}
        with patch.object(lr, 'analyze_report_text', return_value=fields):
            lr.process_location_report_event(row, CONFIG, inbound(text='выручка 100 нал 5 безнал 5'))
        self.store.confirm_draft.assert_not_called()
        self.send_buttons.assert_called_once()

    def test_default_pack_policy_always_confirms(self):
        fields = {'revenue': 100, 'cash': 60, 'non_cash': 40, 'comment': ''}
        with patch.object(lr, 'analyze_report_text', return_value=fields):
            lr.process_location_report_event(ROW, CONFIG, inbound(text='выручка 100'))
        self.send_buttons.assert_called_once()


if __name__ == '__main__':
    unittest.main()
