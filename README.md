# NotiMate Monitor Bot

LINE-бот для русскоязычного владельца бизнеса в Таиланде. Бот читает разрешённые рабочие группы, понимает сообщения и документы на русском, тайском и английском, превращает их в структурированные записи, обновляет Google Sheets и отправляет владельцу уведомления и отчёты на русском языке.

## Языковой контракт

- Интерфейс владельца, уведомления, отчёты и Google Sheets — на русском.
- Входящие тексты и документы — русский, тайский или английский без ручного выбора языка.
- Названия товаров, пояснения и рекомендации нормализуются на русский.

## Как работает

`LINE-группа → Flask webhook → OpenAI Responses API → Google Sheets → уведомление владельцу в LINE`

Модель по умолчанию — `gpt-5.6-luna`; её можно изменить через `OPENAI_MODEL` без правки кода. Для изображений используется тот же мультимодальный API.

## Стек

Python, Flask, LINE SDK v3, OpenAI Responses API, gspread, Railway. Целевая надёжная архитектура с PostgreSQL и отдельным worker описана в корневой документации проекта.

## Настройка

Скопируйте `.env.example`, создайте `clients.json` на основе `clients.json.example` или передайте конфигурацию через `CLIENTS_JSON`. Не добавляйте реальные ключи в Git.

| Переменная | Назначение |
|---|---|
| `OPENAI_API_KEY` | Ключ OpenAI API |
| `OPENAI_MODEL` | Модель; по умолчанию `gpt-5.6-luna` |
| `GOOGLE_CREDENTIALS` | JSON Google service account одной строкой |
| `CLIENTS_JSON` | Конфигурация клиентов одной строкой |
| `PORT` | Порт веб-сервиса |

Конфигурация клиента требует ключ по LINE `destination`/Bot User ID и поля `channel_access_token`, `channel_secret`, `owner_line_id`, `sheet_id`. Поля `name`, `business_type` и `custom_context` передаются модели как контекст конкретного бизнеса.

## Проверка

```bash
python3 -m unittest -v test_core.py test_worker.py test_webhook.py
python3 -m py_compile app.py core.py event_store.py worker.py test_core.py test_worker.py test_webhook.py
```

`/health` проверяет процесс. `/ready` требует доступные Google Sheets и PostgreSQL.

## Устойчивая обработка webhook

Webhook проверяет LINE-подпись, сохраняет каждый `webhookEventId` в PostgreSQL и сразу отвечает LINE. Уникальный первичный ключ не позволяет повторной доставке создать вторую задачу. Медленная обработка выполняется отдельным `worker.py`.

Дополнительные переменные:

| Переменная | Назначение |
|---|---|
| `DATABASE_URL` | PostgreSQL URL, в Railway добавляется сервисом Postgres |
| `WORKER_MAX_ATTEMPTS` | Максимум попыток; по умолчанию 5 |
| `WORKER_POLL_SECONDS` | Интервал опроса очереди; по умолчанию 1 секунда |

Команды процессов:

```text
web: gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 30
worker: python worker.py
```

В Railway нужны два сервиса из одного репозитория: web с первой командой и worker со второй. Оба получают одинаковые переменные, включая `DATABASE_URL`.

Таблица `line_events` создаётся автоматически при запуске и также описана в `migrations/001_line_events.sql`. События после пяти неудачных попыток получают статус `failed` и остаются в PostgreSQL для разбора.

До включения LINE `Webhook redelivery` необходимо развернуть и web, и worker и проверить уникальность события на staging. Повтор всей задачи после частичного сбоя Google Sheets пока может повторить строку: идемпотентность побочных эффектов — следующий обязательный этап.
