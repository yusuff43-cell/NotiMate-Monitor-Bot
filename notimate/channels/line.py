"""LINE Messaging API adapter: webhook signature, client cache, owner push."""

from __future__ import annotations

from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    MessagingApiBlob,
    PushMessageRequest,
    TextMessage,
)

import app
from logging_utils import get_logger

logger = get_logger()

_line_api_cache: dict[str, tuple] = {}


def get_line_clients(token: str):
    if token not in _line_api_cache:
        api_client = ApiClient(Configuration(access_token=token))
        _line_api_cache[token] = (
            MessagingApi(api_client),
            MessagingApiBlob(api_client),
            api_client,
        )
    return _line_api_cache[token]


def get_line_api(token: str) -> MessagingApi:
    return get_line_clients(token)[0]


def get_line_blob_api(token: str) -> MessagingApiBlob:
    return get_line_clients(token)[1]


def verify_signature(client_cfg, body: str, signature: str) -> None:
    """Raise ``InvalidSignatureError`` unless the webhook body matches the client secret."""
    WebhookHandler(client_cfg['channel_secret']).handle(body, signature)


def notify_owner(client_cfg, msg):
    # Channel-neutral tenants (WhatsApp «monitor» pack) route owner messages through their own callable.
    custom = client_cfg.get('_notify')
    if callable(custom):
        try:
            custom(msg)
            return True
        except Exception as exc:
            logger.error('owner_notification_failed', extra={'error_type': type(exc).__name__})
            return False
    try:
        api = app.get_line_api(client_cfg['channel_access_token'])
        recipients = [client_cfg['owner_line_id']]
        if client_cfg.get('owner_line_id_2'):
            recipients.append(client_cfg['owner_line_id_2'])
        for recipient in recipients:
            api.push_message(PushMessageRequest(
                to=recipient,
                messages=[TextMessage(text=msg)],
            ))
        return True
    except Exception as exc:
        logger.error('owner_notification_failed', extra={'error_type': type(exc).__name__})
        return False
