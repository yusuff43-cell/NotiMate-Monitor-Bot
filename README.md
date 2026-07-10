# NotiMate Monitor Bot

Multi-tenant AI-бот для мониторинга рабочих чатов малого бизнеса (LINE).
Владелец бизнеса добавляет бота в рабочую группу — Claude анализирует
сообщения сотрудников (продажи, расходы, остатки, проблемы), пишет
структурированные данные в Google Sheets клиента и шлёт push владельцу.

Один сервис на Railway обслуживает всех клиентов: маршрутизация
по `destination` (Bot User ID), конфигурация клиентов — без изменения кода.
Работал в продакшене с платящим клиентом (Таиланд, 2025–2026).

## Как работает
Сообщение в группу клиента
↓ webhook с destination (Bot User ID клиента)
app.py → find_client(destination) → конфигурация клиента
↓ Claude Haiku анализирует: продажа / расход / сток / проблема
↓ (фото чека → Claude Vision → распознавание суммы)
↓ запись в Google Sheets клиента
↓ push-уведомление владельцу
## Стек

Flask + LINE SDK v2 + Claude Haiku (+ Vision для чеков) + gspread + Railway

## Конфигурация клиентов

Читается из переменной окружения `CLIENTS_JSON` (приоритет)
или из локального файла `clients.json` (см. `clients.json.example`).

## Переменные Railway

| Переменная | Что это |
|---|---|
| `ANTHROPIC_API_KEY` | Ключ Anthropic (общий) |
| `GOOGLE_CREDENTIALS` | JSON service account одной строкой |
| `CLIENTS_JSON` | Конфигурация клиентов одной строкой (опционально) |

## Добавление клиента (без изменения кода)

Добавить блок в конфигурацию:

```json
{
  "BOT_USER_ID_клиента": {
    "name": "Имя бизнеса",
    "business_type": "cafe",
    "custom_context": "описание бизнеса для AI",
    "channel_access_token": "токен OA клиента",
    "channel_secret": "secret OA клиента",
    "owner_line_id": "LINE ID владельца",
    "sheet_id": "ID Google таблицы клиента"
  }
}
```

Deploy на Railway происходит автоматически при push.

## Где брать данные клиента

- **Bot User ID** (ключ) — LINE Developers → Messaging API → Bot User ID
- **channel_access_token** — LINE Developers → Messaging API → Issue
- **channel_secret** — LINE Developers → Basic settings
- **owner_line_id** — LINE User ID владельца
- **sheet_id** — из URL таблицы: docs.google.com/spreadsheets/d/`SHEET_ID`/edit

## Google Sheets доступ

Расшарить таблицу клиента на email service account
(поле `client_email` из GOOGLE_CREDENTIALS) с правами редактора.