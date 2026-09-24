"""«Бухгалтер»: intake of document photos, owner/accountant commands, month package delivery.

Two entry points share one core (``intake_document``):

* WhatsApp tenants whose ``vertical_pack`` is ``accountant`` — ``process_accountant_event``
  (photo → number reply, owner commands, accountant questions);
* LINE tenants that switch the module on with ``modules.accountant.enabled`` (JSC) —
  ``register_line_document`` runs beside the existing photo pipeline and is fully isolated:
  it never raises, and it does nothing at all until the flag is set, so JSC's behaviour is
  unchanged by default (docs/21: «Не меняй поведение для JSC без явного решения»).
"""

from __future__ import annotations

import base64
import datetime as dt
from collections.abc import Mapping
from typing import Any

from notimate.packs.accountant import storage
from notimate.packs.accountant.checks import check_month, summarize_findings
from notimate.packs.accountant.extract import analyze_document_image, document_is_confident
from notimate.packs.accountant.package import build_month_package, previous_period, send_email
from notimate.packs.accountant.rules import DOC_TYPES, rules_for
from notimate.packs.accountant.store import DocumentAlreadyFinalized
from notimate.policy import needs_confirmation, resolve_policy
from notimate.timeutil import local_date

PACKAGE_COMMANDS = ('пакет', 'package', 'пакет месяца')
MISSING_COMMANDS = ('не хватает', 'что не хватает', 'missing')
REGISTRY_COMMANDS = ('документы', 'реестр', 'documents')
HELP_COMMANDS = ('помощь', 'справка', 'help')
DASHBOARD_COMMANDS = ('дашборд', 'dashboard', 'подробный отчёт', 'подробный отчет')
ACCEPT_PREFIXES = ('принято', 'accepted')
QUESTION_PREFIXES = ('вопрос', 'question')

HELP_TEXT = (
    'NotiMate — документы для бухгалтера.\n\n'
    '• Сотрудники: пришлите фото чека, накладной или банковского слипа — бот ответит номером, '
    'его нужно написать на бумажном оригинале.\n'
    '• «Не хватает» — какие документы отсутствуют или под вопросом.\n'
    '• «Пакет» или «Пакет 2026-09» — реестр, фото и список оригиналов архивом.\n'
    '• «Документы» — сколько документов принято за месяц.\n'
    'Бухгалтер: «принято 2026-09» или «вопрос 2026-09-017 текст».'
)


def _normalize(text: str) -> str:
    return text.strip().lower().lstrip('/')


def module_settings(tenant: Mapping[str, Any]) -> dict[str, Any]:
    modules = tenant.get('modules') or {}
    settings = modules.get('accountant') if isinstance(modules, Mapping) else None
    return dict(settings) if isinstance(settings, Mapping) else {}


def module_enabled(tenant: Mapping[str, Any]) -> bool:
    return tenant.get('vertical_pack') == 'accountant' or bool(module_settings(tenant).get('enabled'))


def format_document_line(doc_or_fields: Mapping[str, Any]) -> str:
    total = doc_or_fields.get('total')
    parts = [
        str(doc_or_fields.get('seller') or 'без названия'),
        str(doc_or_fields.get('doc_date') or 'дата не распознана'),
        f"{float(total):,.0f} {doc_or_fields.get('currency') or ''}".replace(',', ' ').strip() if total is not None else 'сумма не распознана',
        DOC_TYPES.get(doc_or_fields.get('doc_type'), 'документ'),
    ]
    return ' · '.join(parts)


def format_confirmed(doc: Mapping[str, Any]) -> str:
    return f"✅ Записал, № {doc['doc_number']} — напишите этот номер на бумажном оригинале.\n{format_document_line(doc)}"


def format_draft(fields: Mapping[str, Any]) -> str:
    return f'🧾 Проверьте документ перед сохранением:\n{format_document_line(fields)}'


def intake_document(
    store, tenant: Mapping[str, Any], sender_id: str, event_id: str, data: bytes, mime: str,
    *, fallback_date: str, confirmed_by: str = '', policy: str | None = None,
) -> dict[str, Any]:
    """Classify, store and (policy permitting) number one document photo.

    Returns ``{'status': ..., ...}`` where status is one of ``exists`` (worker retry of an
    already-handled event — caller must not reply again), ``duplicate`` (same image already
    filed), ``not_document``, ``draft`` or ``confirmed``.
    """
    tenant_id = tenant['id']
    existing = store.find_by_event(tenant_id, event_id)
    if existing:
        return {'status': 'exists', 'doc': existing}
    duplicate = store.find_by_hash(tenant_id, storage.sha256_of(data))
    if duplicate:
        return {'status': 'duplicate', 'doc': duplicate}
    context = ' '.join(filter(None, [str(tenant.get('name') or ''), str(tenant.get('business_type') or '')]))
    fields = analyze_document_image(base64.b64encode(data).decode('ascii'), mime, context)
    if fields is None:
        return {'status': 'not_document'}
    ref, sha = storage.save_original(tenant_id, data, mime)
    doc_id, created = store.create_document(tenant_id, event_id, sender_id, fields, sha, ref, fallback_date)
    if not created:
        return {'status': 'exists', 'doc': store.get_document(doc_id)}
    confident = document_is_confident(fields)
    policy = policy or resolve_policy(tenant, 'accountant', 'document')
    if needs_confirmation(policy, confident):
        return {'status': 'draft', 'doc_id': doc_id, 'fields': fields}
    return {'status': 'confirmed', 'doc': store.confirm_document(doc_id, confirmed_by or 'auto'), 'confident': confident}


# ── findings / package ─────────────────────────────────────────────────────────────────

def compute_findings(store, tenant: Mapping[str, Any], period: str) -> tuple[list[dict], list[dict]]:
    documents = store.list_confirmed(tenant['id'], period)
    operations = store.list_operations(tenant['id'], period)
    settings = module_settings(tenant)
    compare = bool(operations) and settings.get('link_operations', True)
    rules = rules_for(tenant.get('country'), tenant.get('modules'))
    return documents, check_month(documents, operations, rules, compare_operations=bool(compare))


def build_package_for(store, tenant: Mapping[str, Any], period: str) -> tuple[bytes, str]:
    documents, findings = compute_findings(store, tenant, period)
    questions = store.open_questions(tenant['id'], period)
    return build_month_package(str(tenant.get('name') or tenant['id']), period, documents, findings, questions, storage.read_original)


def deliver_package_whatsapp(
    store, tenant: Mapping[str, Any], access_token: str, phone_number_id: str,
    recipients: list[str], period: str,
) -> bool:
    import app
    if not recipients:
        return False
    data, summary = build_package_for(store, tenant, period)
    filename = f"{tenant['id']}-{period}.zip"
    for recipient in recipients:
        app.whatsapp_send_text(access_token, phone_number_id, recipient, summary)
        app.whatsapp_send_document(access_token, phone_number_id, recipient, filename, 'application/zip', data, f'Пакет за {period}')
    emails = module_settings(tenant).get('emails') or []
    try:
        send_email(list(emails), f"NotiMate: пакет документов за {period} — {tenant.get('name') or tenant['id']}", summary, filename, data)
    except Exception as exc:
        from logging_utils import get_logger
        get_logger().warning('accountant_email_failed', extra={'error_type': type(exc).__name__})
    store.mark_period(tenant['id'], period, 'sent')
    return True


def send_scheduled_package(tenant: Mapping[str, Any], access_token: str, phone_number_id: str, recipients: list[str], timezone: str | None) -> None:
    """1st-of-month job: send the previous month's package (docs/19: «1–3 числа: пакет месяца»)."""
    import app
    from logging_utils import get_logger
    store = app.documents_store
    if not store:
        return
    try:
        today = dt.date.fromisoformat(local_date(timezone))
        deliver_package_whatsapp(store, tenant, access_token, phone_number_id, recipients, previous_period(today))
    except Exception as exc:
        get_logger().warning('accountant_package_failed', extra={'error_type': type(exc).__name__})


def send_weekly_missing_digest(tenant: Mapping[str, Any], access_token: str, phone_number_id: str, owner_ids: list[str], timezone: str | None) -> None:
    """Monday digest to the owner: what is missing right now, during the month, not on the due date."""
    import app
    from logging_utils import get_logger
    store = app.documents_store
    if not store:
        return
    try:
        period = local_date(timezone)[:7]
        _, findings = compute_findings(store, tenant, period)
        if not findings:
            return
        for owner_id in owner_ids:
            app.whatsapp_send_text(access_token, phone_number_id, owner_id, summarize_findings(findings))
    except Exception as exc:
        get_logger().warning('accountant_digest_failed', extra={'error_type': type(exc).__name__})


# ── WhatsApp dispatcher ─────────────────────────────────────────────────────────────────

def _period_arg(text: str, default: str) -> str:
    for token in text.split()[1:]:
        if len(token) == 7 and token[4] == '-' and token[:4].isdigit() and token[5:].isdigit():
            return token
    return default


def process_accountant_event(row: dict[str, Any], config: dict[str, Any], inbound) -> None:
    import app
    from logging_utils import get_logger

    tenant = row['tenant']
    tenant_id = tenant['id']
    store = app.documents_store
    access_token, phone_number_id = config['access_token'], config['phone_number_id']
    tz_name = config.get('timezone') or tenant.get('timezone')
    text = inbound.text.strip()
    settings = module_settings(tenant)
    accountant_ids = {str(a) for a in settings.get('accountant_ids') or []}
    owner_ids = [str(o) for o in row['channel'].get('owner_ids') or []]
    is_owner = inbound.sender_role == 'owner'
    is_accountant = inbound.sender_id in accountant_ids

    def reply(body: str) -> None:
        app.whatsapp_send_text(access_token, phone_number_id, inbound.sender_id, body)

    if not store or not app.DOCUMENTS_DB_ENABLED:
        reply('Модуль документов временно недоступен.')
        return

    # Draft buttons
    for prefix in ('doc:confirm:', 'doc:cancel:', 'doc:edit:'):
        if text.startswith(prefix):
            try:
                document_id = int(text[len(prefix):])
            except ValueError:
                return
            try:
                if prefix == 'doc:confirm:':
                    reply(format_confirmed(store.confirm_document(document_id, inbound.sender_id)))
                else:
                    store.reject_document(document_id)
                    reply('Отправьте фото документа ещё раз — лучше при хорошем освещении, целиком в кадре.'
                          if prefix == 'doc:edit:' else 'Документ отменён.')
            except KeyError:
                reply('Черновик уже не найден. Отправьте фото ещё раз.')
            except DocumentAlreadyFinalized:
                reply('Этот документ уже обработан.')
            return

    media = getattr(inbound, 'media', ())
    if media and media[0].get('kind') == 'image':
        try:
            data, mime = app.whatsapp_download_media(access_token, media[0]['id'])
        except Exception as exc:
            get_logger().warning('document_media_failed', extra={'error_type': type(exc).__name__})
            reply('Не удалось получить фото. Отправьте его ещё раз.')
            return
        result = intake_document(store, tenant, inbound.sender_id, inbound.external_event_id, data, mime, fallback_date=local_date(tz_name))
        status = result['status']
        if status == 'exists':
            return
        if status == 'not_document':
            reply('Не вижу на фото документа (чек, накладная, счёт или слип). Пришлите фото документа целиком.')
        elif status == 'duplicate':
            doc = result['doc']
            reply(f"Этот документ уже загружен: № {doc['doc_number']}." if doc.get('doc_number') else 'Этот документ уже загружен и ждёт подтверждения.')
        elif status == 'confirmed':
            reply(format_confirmed(result['doc']))
        else:
            doc_id = result['doc_id']
            app.whatsapp_send_interactive_buttons(
                access_token, phone_number_id, inbound.sender_id, format_draft(result['fields']),
                [(f'doc:confirm:{doc_id}', 'Сохранить'), (f'doc:edit:{doc_id}', 'Переснять'), (f'doc:cancel:{doc_id}', 'Отмена')],
            )
        return

    command = _normalize(text)
    today_period = local_date(tz_name)[:7]

    if is_accountant:
        first = command.split(' ', 1)[0]
        if first in ACCEPT_PREFIXES:
            period = _period_arg(command, previous_period(dt.date.fromisoformat(local_date(tz_name))))
            store.mark_period(tenant_id, period, 'accepted')
            reply(f'Отмечено: пакет за {period} принят.')
            for owner_id in owner_ids:
                app.whatsapp_send_text(access_token, phone_number_id, owner_id, f'✅ Бухгалтер принял пакет за {period}.')
            return
        if first in QUESTION_PREFIXES:
            body = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ''
            if not body:
                reply('Напишите вопрос: «вопрос 2026-09-017 нужен полный tax invoice».')
                return
            number = body.split()[0] if body.split()[0].count('-') == 2 else None
            question = body[len(number):].strip() if number else body
            store.add_question(tenant_id, inbound.sender_id, question or body, number[:7] if number else today_period, number)
            reply('Вопрос передан владельцу.')
            for owner_id in owner_ids:
                app.whatsapp_send_text(access_token, phone_number_id, owner_id, f"❓ Вопрос бухгалтера{f' по №{number}' if number else ''}: {question or body}")
            return

    if is_owner or is_accountant:
        if command.split(' ', 1)[0] in PACKAGE_COMMANDS:
            period = _period_arg(command, previous_period(dt.date.fromisoformat(local_date(tz_name))))
            deliver_package_whatsapp(store, tenant, access_token, phone_number_id, [inbound.sender_id], period)
            return
        if command in MISSING_COMMANDS:
            _, findings = compute_findings(store, tenant, today_period)
            reply(summarize_findings(findings))
            return
        if command in REGISTRY_COMMANDS:
            docs = store.list_confirmed(tenant_id, today_period)
            total = sum(float(d['total']) for d in docs if d.get('total') is not None)
            reply(f'📚 За {today_period} принято документов: {len(docs)}, на сумму {total:,.0f}'.replace(',', ' '))
            return
        if command in DASHBOARD_COMMANDS and is_owner:
            from notimate.dashboard.routes import owner_link
            url = owner_link(tenant, inbound.sender_id)
            reply(f'📊 Подробный отчёт (ссылка действует 5 минут):\n{url}' if url else 'Подробный отчёт пока не подключён.')
            return
        if command in HELP_COMMANDS:
            reply(HELP_TEXT)
            return

    if command in HELP_COMMANDS:
        reply(HELP_TEXT)
        return
    reply('Пришлите фото документа (чек, накладная, счёт, слип). Команды — «помощь».')


# ── LINE (JSC) opt-in hook ──────────────────────────────────────────────────────────────

def register_line_document(destination: str, client_cfg: Mapping[str, Any], event: Mapping[str, Any], data: bytes) -> None:
    """File one LINE photo in the document registry when the tenant enabled the module.

    Never raises: any failure here is logged and swallowed so the existing expense/shift
    processing of the same photo (and the worker's retry accounting) is unaffected.
    """
    import app
    from logging_utils import get_logger
    from notimate.timeutil import bangkok_date

    settings = (client_cfg.get('modules') or {}).get('accountant') if isinstance(client_cfg.get('modules'), Mapping) else None
    if not (isinstance(settings, Mapping) and settings.get('enabled')):
        return
    store = app.documents_store
    if not store or not app.DOCUMENTS_DB_ENABLED:
        return
    try:
        tenant = {
            'id': destination, 'name': client_cfg.get('name'), 'business_type': client_cfg.get('business_type'),
            'country': client_cfg.get('country') or 'TH', 'modules': client_cfg.get('modules'), 'vertical_pack': None,
        }
        source = event.get('source', {})
        result = intake_document(
            store, tenant, str(source.get('userId') or ''), str(event.get('webhookEventId')), data, 'image/jpeg',
            fallback_date=bangkok_date(), policy='auto',  # no button UI in the LINE pipeline: always file, flag doubts
        )
        if result['status'] == 'confirmed' and not result.get('confident', True):
            app.notify_owner(client_cfg, f"🧾 Документ № {result['doc']['doc_number']} распознан неуверенно — сверьте с оригиналом:\n{format_document_line(result['doc'])}")
        if result['status'] == 'confirmed' and settings.get('reply_in_chat', True):
            target = source.get('groupId') or source.get('roomId') or source.get('userId')
            if target:
                from linebot.v3.messaging import PushMessageRequest, TextMessage
                app.get_line_api(client_cfg['channel_access_token']).push_message(
                    PushMessageRequest(to=target, messages=[TextMessage(text=format_confirmed(result['doc']))])
                )
    except Exception as exc:
        get_logger().warning('line_document_registration_failed', extra={'error_type': type(exc).__name__})


def line_owner_command(destination: str, client_cfg: Mapping[str, Any], user_id: str, command: str) -> bool:
    """Owner DM commands added for LINE tenants that opted in (JSC's existing commands are
    untouched): «дашборд» → login link, «пакет [YYYY-MM]» → package download link.
    Returns True when the command was handled."""
    import app
    from notimate.dashboard import routes

    word = command.strip().lower().split(' ', 1)[0]
    tenant = {'id': destination, 'name': client_cfg.get('name'), 'modules': client_cfg.get('modules'), 'vertical_pack': None}
    modules = client_cfg.get('modules') if isinstance(client_cfg.get('modules'), Mapping) else {}
    if word in ('дашборд', 'dashboard') and routes.link_enabled_for(tenant):
        url = routes.owner_link(tenant, user_id)
        app.notify_owner(client_cfg, f'📊 Подробный отчёт (ссылка действует 5 минут):\n{url}')
        return True
    if word in ('пакет', 'package') and isinstance(modules.get('accountant'), Mapping) and modules['accountant'].get('enabled') and routes.feature_enabled():
        period = _period_arg(command.strip().lower(), previous_period(dt.date.fromisoformat(local_date('Asia/Bangkok'))))
        url = routes.package_link(tenant, user_id, period)
        app.notify_owner(client_cfg, f'📦 Пакет документов за {period} (ссылка действует 15 минут):\n{url}')
        return True
    return False
