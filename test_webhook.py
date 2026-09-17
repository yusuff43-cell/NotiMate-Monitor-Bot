import base64
import hashlib
import hmac
import importlib
import json
import os
import sys
import unittest
from unittest.mock import Mock


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


if __name__ == '__main__':
    unittest.main()
