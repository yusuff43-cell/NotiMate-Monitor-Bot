"""notimate/inbound.py: build_whatsapp_inbound_message normalizes a tenant_store row +
raw WhatsApp message into the channel-agnostic InboundMessage contract (Этап 3, docs/21)."""

import unittest

from notimate.inbound import InboundMessage, build_whatsapp_inbound_message

ROW = {
    'tenant': {'id': 'erzhan-3biz', 'sheet_id': 'sheet-1', 'name': 'Ержан', 'business_type': None, 'custom_context': None},
    'channel': {'channel': 'whatsapp', 'external_id': 'PNID-1', 'secret_ref': 'PNID-1', 'owner_ids': ['77009998877'], 'allowed_chats': None},
}


class BuildWhatsappInboundMessageTests(unittest.TestCase):
    def test_text_message_from_owner(self):
        message = {'from': '77009998877', 'id': 'wamid.ONE', 'type': 'text', 'text': {'body': 'молоко 3'}}
        result = build_whatsapp_inbound_message(ROW, message)
        self.assertIsInstance(result, InboundMessage)
        self.assertEqual(result.tenant_id, 'erzhan-3biz')
        self.assertEqual(result.channel, 'whatsapp')
        self.assertEqual(result.external_event_id, 'wamid.ONE')
        self.assertEqual(result.chat_id, '77009998877')
        self.assertEqual(result.sender_id, '77009998877')
        self.assertEqual(result.sender_role, 'owner')
        self.assertEqual(result.text, 'молоко 3')

    def test_text_message_from_staff_is_not_owner(self):
        message = {'from': '77001112233', 'id': 'wamid.TWO', 'type': 'text', 'text': {'body': 'привет'}}
        result = build_whatsapp_inbound_message(ROW, message)
        self.assertEqual(result.sender_role, 'staff')

    def test_interactive_button_reply_uses_button_id_as_text(self):
        message = {
            'from': '77009998877', 'id': 'wamid.THREE', 'type': 'interactive',
            'interactive': {'type': 'button_reply', 'button_reply': {'id': 'draft:save', 'title': 'Сохранить'}},
        }
        result = build_whatsapp_inbound_message(ROW, message)
        self.assertEqual(result.text, 'draft:save')

    def test_interactive_list_reply_uses_list_id_as_text(self):
        message = {
            'from': '77009998877', 'id': 'wamid.FOUR', 'type': 'interactive',
            'interactive': {'type': 'list_reply', 'list_reply': {'id': 'storepick:menu:1', 'title': 'Магазин'}},
        }
        result = build_whatsapp_inbound_message(ROW, message)
        self.assertEqual(result.text, 'storepick:menu:1')

    def test_unsupported_message_type_has_empty_text(self):
        message = {'from': '77009998877', 'id': 'wamid.FIVE', 'type': 'image', 'image': {'id': 'media-1'}}
        result = build_whatsapp_inbound_message(ROW, message)
        self.assertEqual(result.text, '')

    def test_timestamp_is_parsed_to_utc_datetime(self):
        message = {'from': '77009998877', 'id': 'wamid.SIX', 'type': 'text', 'text': {'body': 'x'}, 'timestamp': '1758600000'}
        result = build_whatsapp_inbound_message(ROW, message)
        self.assertIsNotNone(result.received_at)
        self.assertEqual(result.received_at.year, 2025)

    def test_missing_timestamp_leaves_received_at_none(self):
        message = {'from': '77009998877', 'id': 'wamid.SEVEN', 'type': 'text', 'text': {'body': 'x'}}
        result = build_whatsapp_inbound_message(ROW, message)
        self.assertIsNone(result.received_at)


if __name__ == '__main__':
    unittest.main()
