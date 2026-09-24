"""«Монитор» для WhatsApp — тот же принцип, что у LINE-бота JSC, но без групп.

Сотрудник (или владелец) пишет боту в личном чате: фото чека или накладной, PDF, отчёт
о смене, список закупок, остатки, сообщение о проблеме. Бот разбирает сообщение той же
логикой, что и LINE (``notimate.pipeline.handle_text/handle_image``), пишет по вкладкам
Google Sheets клиента и в PostgreSQL, отвечает сотруднику коротким подтверждением и
уведомляет владельцев. Владелец получает те же сводки и команды, что в LINE.

Доступ: писать могут только владельцы (``tenant_channels.owner_ids``) и номера из
``tenant_channels.allowed_chats`` (список сотрудников). Остальным бот отвечает, что доступ
не настроен, и ничего не обрабатывает — в WhatsApp нет «рабочей группы», поэтому список
разрешённых номеров заменяет allowlist групп LINE.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MONEY_COMMANDS = ('деньги', 'финансы', 'money')
SUMMARY_COMMANDS = ('сводка', 'отчет', 'отчёт', 'подробный отчёт', 'подробный отчет', 'report', 'summary')
REMINDER_COMMANDS = ('напоминания', 'reminders')
WEEK_COMMANDS = ('неделя', 'week', 'недельная')
DASHBOARD_COMMANDS = ('дашборд', 'dashboard')
HELP_COMMANDS = ('помощь', 'справка', 'help', 'меню', 'menu')

HELP_TEXT = (
    'NotiMate — мониторинг бизнеса.\n\n'
    'Сотрудники: пришлите фото чека, накладной, отчёта смены или PDF; либо напишите текстом '
    '(«купили молоко 200», «остаток сыр 3 кг», «сломался холодильник») — бот запишет всё в таблицу.\n\n'
    'Владелец: «Деньги» — выручка и расходы, «Отчёт» — подробный отчёт, «Напоминания», «Неделя», '
    '«Дашборд» — ссылка на панель с цифрами, «Не хватает» и «Пакет» — документы для бухгалтера.'
)


def _normalize(text: str) -> str:
    return text.strip().lower().lstrip('/')


def build_cfg(row: Mapping[str, Any], config: Mapping[str, Any], notify) -> dict[str, Any]:
    """A ``client_cfg``-shaped dict so the LINE-era handlers, Sheets writers and reports
    work unchanged; ``_notify`` routes their «owner notification» to WhatsApp."""
    tenant = row['tenant']
    modules = tenant.get('modules') if isinstance(tenant.get('modules'), Mapping) else {}
    from notimate.accountant_defaults import currency_for
    return {
        'name': tenant.get('name'),
        'business_type': tenant.get('business_type'),
        'custom_context': tenant.get('custom_context'),
        'sheet_id': tenant.get('sheet_id'),
        'country': tenant.get('country'),
        'timezone': config.get('timezone') or tenant.get('timezone') or 'Asia/Almaty',
        'currency': modules.get('currency') or currency_for(tenant.get('country')),
        'glossary': modules.get('glossary', 'none'),
        'modules': modules,
        '_notify': notify,
    }


def allowed_senders(row: Mapping[str, Any]) -> set[str]:
    channel = row['channel']
    return {str(o) for o in (channel.get('owner_ids') or [])} | {str(a) for a in (channel.get('allowed_chats') or [])}


def process_monitor_event(row: dict[str, Any], config: dict[str, Any], inbound) -> None:
    import app
    from logging_utils import get_logger
    from notimate import pipeline
    from notimate.packs.accountant import flow as accountant_flow
    from notimate.timeutil import local_date

    logger = get_logger()
    tenant = row['tenant']
    tenant_id = tenant['id']
    access_token, phone_number_id = config['access_token'], config['phone_number_id']
    owner_ids = [str(o) for o in row['channel'].get('owner_ids') or []]
    is_owner = inbound.sender_role == 'owner'

    def send(recipient: str, body: str) -> None:
        app.whatsapp_send_text(access_token, phone_number_id, recipient, body)

    def notify_owners(body: str) -> None:
        for owner_id in owner_ids:
            send(owner_id, body)

    def reply(body: str) -> None:
        send(inbound.sender_id, body)

    if inbound.sender_id not in allowed_senders(row):
        reply('Доступ не настроен для этого номера. Обратитесь к владельцу.')
        return
    if not (app.SHEETS_ENABLED and tenant.get('sheet_id')):
        reply('Таблица клиента пока не подключена — запись невозможна. Сообщите владельцу.')
        return

    cfg = build_cfg(row, config, notify_owners)
    text = inbound.text.strip()
    media = [m for m in getattr(inbound, 'media', ()) if m.get('kind') in ('image', 'pdf')]

    if not media:
        command = _normalize(text)
        if is_owner:
            if command in MONEY_COMMANDS:
                app.evening_summary(cfg, tenant_id=tenant_id)
                return
            if command in SUMMARY_COMMANDS:
                app.detailed_report(cfg)
                return
            if command in REMINDER_COMMANDS:
                app.reminders_report(cfg)
                return
            if command in WEEK_COMMANDS:
                app.weekly_report(cfg)
                return
            if command in DASHBOARD_COMMANDS:
                from notimate.dashboard.routes import owner_link
                url = owner_link(tenant, inbound.sender_id)
                reply(f'📊 Панель с цифрами (ссылка действует 5 минут):\n{url}' if url else 'Панель пока не подключена.')
                return
            if accountant_flow.module_enabled(tenant) and (command.split(' ', 1)[0] in accountant_flow.PACKAGE_COMMANDS or command in accountant_flow.MISSING_COMMANDS):
                accountant_flow.process_accountant_event(row, config, inbound)
                return
        if command in HELP_COMMANDS:
            reply(HELP_TEXT)
            return
        if not text:
            return
        status = pipeline.handle_text(tenant_id, cfg, inbound.external_event_id, text)
        if status != 'ignored':
            reply('✅ Принято, записал в таблицу.')
        return

    try:
        data, mime = app.whatsapp_download_media(access_token, media[0]['id'])
    except Exception as exc:
        logger.warning('monitor_media_failed', extra={'error_type': type(exc).__name__})
        reply('Не удалось получить файл. Отправьте его ещё раз.')
        return

    doc_line = ''
    if accountant_flow.module_enabled(tenant):
        # Same file into the numbered document registry (Этап 7); the expense/shift recording
        # below runs regardless, so a registry problem never blocks the daily bookkeeping.
        try:
            result = accountant_flow.intake_document(
                app.documents_store, {**tenant, 'name': tenant.get('name')}, inbound.sender_id,
                inbound.external_event_id, data, mime, fallback_date=local_date(cfg['timezone']), policy='auto',
            ) if app.documents_store and app.DOCUMENTS_DB_ENABLED else {'status': 'skipped'}
            if result['status'] == 'confirmed':
                doc_line = f"\n🧾 Документ № {result['doc']['doc_number']} — напишите этот номер на оригинале."
            elif result['status'] == 'duplicate' and result['doc'].get('doc_number'):
                reply(f"Этот документ уже загружен: № {result['doc']['doc_number']}.")
                return
        except Exception as exc:
            logger.warning('monitor_document_registry_failed', extra={'error_type': type(exc).__name__})

    status = pipeline.handle_image(tenant_id, cfg, inbound.external_event_id, data, mime)
    if status == 'ignored' and not doc_line:
        reply('Не вижу на фото финансового документа (чек, накладная, отчёт смены). Пришлите документ целиком.')
    else:
        reply('✅ Принято, записал в таблицу.' + doc_line)
