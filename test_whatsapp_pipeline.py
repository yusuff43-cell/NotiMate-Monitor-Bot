"""process_whatsapp_event: resolves the tenant, builds InboundMessage, sends an ack reply.

Этап 3 MVP per docs/21 — real business logic (drafts, «Отчёты точек») is Этап 6; this only
proves the adapter → tenant resolution → reply loop works, exactly like process_line_event
does for LINE.
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

ROW = {
    'tenant': {'id': 'erzhan-3biz', 'sheet_id': 'sheet-1', 'name': 'Ержан', 'business_type': None, 'custom_context': None},
    'channel': {'channel': 'whatsapp', 'external_id': 'PNID-1', 'secret_ref': 'PNID-1', 'owner_ids': ['77009998877'], 'allowed_chats': None},
}


class ProcessWhatsappEventTests(unittest.TestCase):
    def setUp(self):
        self.original_secrets = app_module.WHATSAPP_SECRETS
        self.addCleanup(setattr, app_module, 'WHATSAPP_SECRETS', self.original_secrets)
        app_module.WHATSAPP_SECRETS = {'PNID-1': {'access_token': 'tok-1'}}
        self.find_channel = patch.object(app_module, 'find_whatsapp_channel', return_value=ROW).start()
        self.send_text = patch.object(app_module, 'whatsapp_send_text').start()
        self.addCleanup(patch.stopall)

    def test_unknown_phone_number_id_raises(self):
        self.find_channel.return_value = None
        with self.assertRaisesRegex(ValueError, 'Unknown WhatsApp'):
            app_module.process_whatsapp_event('PNID-unknown', {'from': 'x', 'id': 'wamid.1', 'type': 'text', 'text': {'body': 'hi'}})

    def test_missing_secret_raises(self):
        app_module.WHATSAPP_SECRETS = {}
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            app_module.process_whatsapp_event('PNID-1', {'from': '77009998877', 'id': 'wamid.1', 'type': 'text', 'text': {'body': 'hi'}})
        self.send_text.assert_not_called()

    def test_text_message_gets_an_acknowledgement_reply(self):
        message = {'from': '77009998877', 'id': 'wamid.1', 'type': 'text', 'text': {'body': 'молоко 3'}}
        app_module.process_whatsapp_event('PNID-1', message)
        self.send_text.assert_called_once_with('tok-1', 'PNID-1', '77009998877', 'Получено: молоко 3')

    def test_empty_text_sends_nothing(self):
        message = {'from': '77009998877', 'id': 'wamid.1', 'type': 'image', 'image': {'id': 'x'}}
        app_module.process_whatsapp_event('PNID-1', message)
        self.send_text.assert_not_called()


if __name__ == '__main__':
    unittest.main()
