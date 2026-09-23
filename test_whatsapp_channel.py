"""notimate/channels/whatsapp.py: signature check, webhook parsing, 24h window, sending.

Pure functions, no network and no app_module import needed — this adapter isn't wired into
any Flask route yet (Этап 3 of docs/21 starts once Meta payment/verification is done).
"""

import datetime as dt
import hashlib
import hmac
import json
import unittest
from unittest.mock import patch

from notimate.channels.whatsapp import (
    configure_conversational_automation,
    extract_messages,
    is_within_free_form_window,
    send_interactive_buttons,
    send_text,
    verify_signature,
    verify_webhook_challenge,
)

APP_SECRET = 'test-app-secret'


def sign(body: bytes) -> str:
    return 'sha256=' + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


# A realistic (trimmed) Cloud API webhook body: one text message plus one delivery status,
# across two tenants' phone numbers to prove extract_messages keeps them apart.
WEBHOOK_PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "waba-1",
            "changes": [
                {
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"display_phone_number": "77001112233", "phone_number_id": "PNID-1"},
                        "messages": [
                            {
                                "from": "77009998877",
                                "id": "wamid.ONE",
                                "timestamp": "1758600000",
                                "type": "text",
                                "text": {"body": "молоко 3"},
                            }
                        ],
                    },
                    "field": "messages",
                }
            ],
        },
        {
            "id": "waba-2",
            "changes": [
                {
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"display_phone_number": "77004445566", "phone_number_id": "PNID-2"},
                        "statuses": [
                            {"id": "wamid.STATUS", "status": "delivered", "recipient_id": "77009998877"}
                        ],
                    },
                    "field": "messages",
                }
            ],
        },
    ],
}


class SignatureTests(unittest.TestCase):
    def test_valid_signature_is_accepted(self):
        body = b'{"a":1}'
        self.assertTrue(verify_signature(APP_SECRET, body, sign(body)))

    def test_tampered_body_is_rejected(self):
        body = b'{"a":1}'
        self.assertFalse(verify_signature(APP_SECRET, b'{"a":2}', sign(body)))

    def test_missing_prefix_is_rejected(self):
        body = b'{"a":1}'
        bare_hex = sign(body).removeprefix('sha256=')
        self.assertFalse(verify_signature(APP_SECRET, body, bare_hex))

    def test_wrong_secret_is_rejected(self):
        body = b'{"a":1}'
        self.assertFalse(verify_signature('other-secret', body, sign(body)))


class WebhookChallengeTests(unittest.TestCase):
    def test_matching_token_returns_challenge(self):
        self.assertEqual(
            verify_webhook_challenge('secret-token', 'subscribe', 'secret-token', 'echo-me'),
            'echo-me',
        )

    def test_wrong_token_is_rejected(self):
        self.assertIsNone(verify_webhook_challenge('secret-token', 'subscribe', 'guess', 'echo-me'))

    def test_wrong_mode_is_rejected(self):
        self.assertIsNone(verify_webhook_challenge('secret-token', 'unsubscribe', 'secret-token', 'echo-me'))


class ExtractMessagesTests(unittest.TestCase):
    def test_extracts_only_messages_not_statuses(self):
        messages = extract_messages(WEBHOOK_PAYLOAD)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['phone_number_id'], 'PNID-1')
        self.assertEqual(messages[0]['message']['id'], 'wamid.ONE')
        self.assertEqual(messages[0]['message']['text']['body'], 'молоко 3')

    def test_two_tenants_phone_number_ids_never_mix(self):
        # PNID-2's entry only carries a status, but proves the two tenants' metadata
        # stay attached to their own entry and are never swapped or merged.
        messages = extract_messages(WEBHOOK_PAYLOAD)
        phone_number_ids = {m['phone_number_id'] for m in messages}
        self.assertEqual(phone_number_ids, {'PNID-1'})

    def test_empty_payload_returns_no_messages(self):
        self.assertEqual(extract_messages({}), [])

    def test_malformed_entry_is_skipped_not_crashed(self):
        self.assertEqual(extract_messages({'entry': [{'changes': [{'value': {}}]}]}), [])


class FreeFormWindowTests(unittest.TestCase):
    def test_no_prior_inbound_message_is_outside_window(self):
        self.assertFalse(is_within_free_form_window(None, dt.datetime.now(dt.timezone.utc)))

    def test_recent_message_is_inside_window(self):
        now = dt.datetime(2026, 9, 23, 12, 0, tzinfo=dt.timezone.utc)
        last = now - dt.timedelta(hours=23, minutes=59)
        self.assertTrue(is_within_free_form_window(last, now))

    def test_message_older_than_24h_is_outside_window(self):
        now = dt.datetime(2026, 9, 23, 12, 0, tzinfo=dt.timezone.utc)
        last = now - dt.timedelta(hours=24, minutes=1)
        self.assertFalse(is_within_free_form_window(last, now))

    def test_naive_datetimes_are_treated_as_utc(self):
        now = dt.datetime(2026, 9, 23, 12, 0)
        last = dt.datetime(2026, 9, 23, 1, 0)
        self.assertTrue(is_within_free_form_window(last, now))


class SendingTests(unittest.TestCase):
    def test_send_text_posts_expected_graph_api_request(self):
        captured = {}

        class FakeResponse:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def read(self_inner):
                return b'{"messages":[{"id":"wamid.OUT"}]}'

        def fake_urlopen(request, timeout=None):
            captured['url'] = request.full_url
            captured['headers'] = dict(request.header_items())
            captured['body'] = json.loads(request.data.decode('utf-8'))
            return FakeResponse()

        with patch('notimate.channels.whatsapp.urllib.request.urlopen', side_effect=fake_urlopen):
            result = send_text('tok-123', 'PNID-1', '77009998877', 'Готово')

        self.assertEqual(result['messages'][0]['id'], 'wamid.OUT')
        self.assertIn('PNID-1/messages', captured['url'])
        self.assertEqual(captured['headers']['Authorization'], 'Bearer tok-123')
        self.assertEqual(captured['body']['to'], '77009998877')
        self.assertEqual(captured['body']['type'], 'text')
        self.assertEqual(captured['body']['text']['body'], 'Готово')
        self.assertEqual(captured['body']['messaging_product'], 'whatsapp')

    def test_send_interactive_buttons_builds_reply_buttons(self):
        captured = {}

        class FakeResponse:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def read(self_inner):
                return b'{}'

        def fake_urlopen(request, timeout=None):
            captured['body'] = json.loads(request.data.decode('utf-8'))
            return FakeResponse()

        with patch('notimate.channels.whatsapp.urllib.request.urlopen', side_effect=fake_urlopen):
            send_interactive_buttons(
                'tok', 'PNID-1', '77009998877', 'Подтвердите',
                [('draft:save', 'Сохранить'), ('draft:cancel', 'Отмена')],
            )

        buttons = captured['body']['interactive']['action']['buttons']
        self.assertEqual(len(buttons), 2)
        self.assertEqual(buttons[0], {'type': 'reply', 'reply': {'id': 'draft:save', 'title': 'Сохранить'}})

    def test_rejects_too_many_buttons(self):
        with self.assertRaises(ValueError):
            send_interactive_buttons('tok', 'PNID-1', '77009998877', 'text', [('a', '1'), ('b', '2'), ('c', '3'), ('d', '4')])

    def test_rejects_zero_buttons(self):
        with self.assertRaises(ValueError):
            send_interactive_buttons('tok', 'PNID-1', '77009998877', 'text', [])

    def test_http_error_is_wrapped_with_response_body(self):
        import urllib.error

        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 401, 'Unauthorized',
                hdrs=None, fp=__import__('io').BytesIO(b'{"error":"bad token"}'),
            )

        with patch('notimate.channels.whatsapp.urllib.request.urlopen', side_effect=fake_urlopen):
            with self.assertRaisesRegex(RuntimeError, 'bad token'):
                send_text('bad-token', 'PNID-1', '77009998877', 'hi')


class ConversationalAutomationTests(unittest.TestCase):
    def _capture(self):
        captured = {}

        class FakeResponse:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def read(self_inner):
                return b'{"success":true}'

        def fake_urlopen(request, timeout=None):
            captured['url'] = request.full_url
            captured['body'] = json.loads(request.data.decode('utf-8'))
            return FakeResponse()

        return captured, fake_urlopen

    def test_posts_commands_and_prompts_to_conversational_automation(self):
        captured, fake_urlopen = self._capture()
        with patch('notimate.channels.whatsapp.urllib.request.urlopen', side_effect=fake_urlopen):
            configure_conversational_automation(
                'tok', 'PNID-1',
                commands=[('summary', 'Сводка по точкам сегодня'), ('missing', 'Кто ещё не отчитался')],
                prompts=['Сводка по точкам', 'Кто не отчитался'],
            )
        self.assertIn('PNID-1/conversational_automation', captured['url'])
        self.assertEqual(captured['body']['commands'], [
            {'command_name': 'summary', 'command_description': 'Сводка по точкам сегодня'},
            {'command_name': 'missing', 'command_description': 'Кто ещё не отчитался'},
        ])
        self.assertEqual(captured['body']['prompts'], ['Сводка по точкам', 'Кто не отчитался'])
        self.assertNotIn('enable_welcome_message', captured['body'])

    def test_omitted_fields_are_not_sent(self):
        captured, fake_urlopen = self._capture()
        with patch('notimate.channels.whatsapp.urllib.request.urlopen', side_effect=fake_urlopen):
            configure_conversational_automation('tok', 'PNID-1', enable_welcome_message=True)
        self.assertEqual(captured['body'], {'enable_welcome_message': True})

    def test_rejects_more_than_30_commands(self):
        with self.assertRaises(ValueError):
            configure_conversational_automation('tok', 'PNID-1', commands=[(str(i), 'x') for i in range(31)])

    def test_rejects_more_than_4_prompts(self):
        with self.assertRaises(ValueError):
            configure_conversational_automation('tok', 'PNID-1', prompts=['a', 'b', 'c', 'd', 'e'])


if __name__ == '__main__':
    unittest.main()
