import base64
import hashlib
import hmac
import importlib
import json
import os
import sys
import unittest
from unittest.mock import Mock, patch


os.environ.setdefault('OPENAI_API_KEY', 'test-key')
os.environ.setdefault('GOOGLE_CREDENTIALS', '{}')
os.environ.setdefault('DISABLE_SCHEDULER', '1')
os.environ.setdefault(
    'CLIENTS_JSON',
    json.dumps({
        'Ubot': {
            'channel_access_token': 'test-token',
            'channel_secret': 'test-secret',
            'owner_line_id': 'Uowner',
            'sheet_id': 'test-sheet',
        }
    }),
)

app_module = importlib.import_module('app')


def signature(body: str) -> str:
    digest = hmac.new(b'test-secret', body.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def payload():
    return {
        'destination': 'Ubot',
        'events': [{
            'type': 'message',
            'webhookEventId': 'evt-1',
            'deliveryContext': {'isRedelivery': False},
            'timestamp': 1770000000000,
            'source': {'type': 'group', 'groupId': 'Cgroup', 'userId': 'Ustaff'},
            'replyToken': '0' * 32,
            'mode': 'active',
            'message': {'type': 'text', 'id': '1234567890', 'quoteToken': 'q', 'text': 'need milk 2'},
        }],
    }


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        self.store = Mock()
        self.store.register_events.return_value = (1, 0)
        app_module.event_store = self.store
        app_module.DB_ENABLED = True

    def post(self, data, valid=True):
        body = json.dumps(data, separators=(',', ':'))
        sig = signature(body) if valid else 'bad'
        return self.client.post('/webhook', data=body, headers={'X-Line-Signature': sig}, content_type='application/json')

    def test_valid_event_is_registered_and_returns_quick_success(self):
        data = payload()
        response = self.post(data)
        self.assertEqual(response.status_code, 200)
        self.store.register_events.assert_called_once_with('Ubot', data['events'])

    def test_duplicate_delivery_still_returns_success(self):
        self.store.register_events.return_value = (0, 1)
        response = self.post(payload())
        self.assertEqual(response.status_code, 200)
        self.store.register_events.assert_called_once()

    def test_invalid_signature_is_rejected(self):
        response = self.post(payload(), valid=False)
        self.assertEqual(response.status_code, 400)
        self.store.register_events.assert_not_called()

    def test_database_outage_returns_retryable_error(self):
        app_module.DB_ENABLED = False
        response = self.post(payload())
        self.assertEqual(response.status_code, 503)
        self.store.register_events.assert_not_called()

    def test_owner_notification_does_not_attach_quick_reply(self):
        api = Mock()
        with patch.object(app_module, 'get_line_api', return_value=api):
            sent = app_module.notify_owner(app_module.CLIENTS['Ubot'], 'Готовый отчёт')
        self.assertTrue(sent)
        request = api.push_message.call_args.args[0]
        self.assertIsNone(request.messages[0].quick_reply)

    def test_openai_usage_is_aggregated_without_recording_content(self):
        response = Mock()
        response.output_text = 'Готово'
        response.usage.input_tokens = 123
        response.usage.output_tokens = 45
        response.usage.output_tokens_details.reasoning_tokens = 0
        client = Mock()
        client.responses.create.return_value = response
        app_module.openai_client = client

        self.assertEqual(app_module.ask_openai('private instruction', 'private input', 100), 'Готово')
        self.store.record_openai_usage.assert_called_once_with(app_module.OPENAI_MODEL, 123, 45, 0)

    def test_openai_usage_accounting_failure_does_not_hide_result(self):
        response = Mock()
        response.output_text = 'Готово'
        response.usage.input_tokens = 1
        response.usage.output_tokens = 2
        response.usage.output_tokens_details.reasoning_tokens = 0
        app_module.openai_client = Mock()
        app_module.openai_client.responses.create.return_value = response
        self.store.record_openai_usage.side_effect = RuntimeError('database unavailable')

        self.assertEqual(app_module.ask_openai('secret', 'message', 100), 'Готово')


if __name__ == '__main__':
    unittest.main()
