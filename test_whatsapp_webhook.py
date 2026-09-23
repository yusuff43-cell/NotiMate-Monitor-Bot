"""/webhook/whatsapp: signature check, GET challenge handshake, idempotent registration."""

import hashlib
import hmac
import importlib
import json
import os
import unittest
from unittest.mock import Mock

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

APP_SECRET = 'wa-app-secret'


def sign(body: bytes) -> str:
    return 'sha256=' + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def payload():
    return {
        'entry': [{
            'changes': [{
                'value': {
                    'metadata': {'phone_number_id': 'PNID-1'},
                    'messages': [{'from': '77009998877', 'id': 'wamid.ONE', 'type': 'text', 'text': {'body': 'молоко 3'}}],
                },
            }],
        }],
    }


class WhatsappWebhookTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        self.original_app_secret = app_module.WHATSAPP_APP_SECRET
        self.original_verify_token = app_module.WHATSAPP_WEBHOOK_VERIFY_TOKEN
        self.original_store = app_module.whatsapp_inbound_store
        self.original_db_enabled = app_module.WHATSAPP_DB_ENABLED
        app_module.WHATSAPP_APP_SECRET = APP_SECRET
        app_module.WHATSAPP_WEBHOOK_VERIFY_TOKEN = 'verify-me'
        self.store = Mock()
        self.store.register_events.return_value = (1, 0)
        app_module.whatsapp_inbound_store = self.store
        app_module.WHATSAPP_DB_ENABLED = True
        self.addCleanup(setattr, app_module, 'WHATSAPP_APP_SECRET', self.original_app_secret)
        self.addCleanup(setattr, app_module, 'WHATSAPP_WEBHOOK_VERIFY_TOKEN', self.original_verify_token)
        self.addCleanup(setattr, app_module, 'whatsapp_inbound_store', self.original_store)
        self.addCleanup(setattr, app_module, 'WHATSAPP_DB_ENABLED', self.original_db_enabled)

    def post(self, data, valid=True):
        body = json.dumps(data, separators=(',', ':')).encode('utf-8')
        sig = sign(body) if valid else 'sha256=deadbeef'
        return self.client.post('/webhook/whatsapp', data=body, headers={'X-Hub-Signature-256': sig}, content_type='application/json')

    # -- GET challenge --------------------------------------------------------------------

    def test_get_with_matching_token_echoes_challenge(self):
        response = self.client.get('/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=verify-me&hub.challenge=echo123')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), 'echo123')

    def test_get_with_wrong_token_is_rejected(self):
        response = self.client.get('/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=guess&hub.challenge=echo123')
        self.assertEqual(response.status_code, 403)

    # -- POST signature ---------------------------------------------------------------------

    def test_valid_signature_registers_event(self):
        response = self.post(payload())
        self.assertEqual(response.status_code, 200)
        self.store.register_events.assert_called_once_with('PNID-1', [payload()['entry'][0]['changes'][0]['value']['messages'][0]])

    def test_invalid_signature_is_rejected(self):
        response = self.post(payload(), valid=False)
        self.assertEqual(response.status_code, 403)
        self.store.register_events.assert_not_called()

    def test_missing_app_secret_rejects_everything(self):
        app_module.WHATSAPP_APP_SECRET = ''
        response = self.post(payload())
        self.assertEqual(response.status_code, 403)
        self.store.register_events.assert_not_called()

    def test_duplicate_delivery_still_returns_ok(self):
        self.store.register_events.return_value = (0, 1)
        response = self.post(payload())
        self.assertEqual(response.status_code, 200)

    def test_database_unavailable_returns_503(self):
        app_module.WHATSAPP_DB_ENABLED = False
        response = self.post(payload())
        self.assertEqual(response.status_code, 503)
        self.store.register_events.assert_not_called()

    def test_status_only_payload_registers_nothing_but_still_ok(self):
        status_payload = {
            'entry': [{'changes': [{'value': {
                'metadata': {'phone_number_id': 'PNID-1'},
                'statuses': [{'id': 'wamid.STATUS', 'status': 'delivered'}],
            }}]}],
        }
        response = self.post(status_payload)
        self.assertEqual(response.status_code, 200)
        self.store.register_events.assert_not_called()


if __name__ == '__main__':
    unittest.main()
