# Запуск Monitor Bot на VPS

Корневая папка: `/home/hermes/apps/notimate-monitor`. Секреты живут только в
`/home/hermes/apps/notimate-monitor/.env`; запускаемый код — в неизменяемом
каталоге `releases/<commit>`.

Состав: PostgreSQL 16, web на `127.0.0.1:8084`, отдельный worker, Nginx и ежедневный PostgreSQL backup.

## Секреты

Скопировать `.env.vps.example` в `.env`, заполнить реальные значения и установить права `600`. Для `POSTGRES_PASSWORD` использовать URI-безопасную hex-строку:

```bash
openssl rand -hex 32
```

Не отправлять `.env` в Git или чат.

## Первый запуск

```bash
docker compose -f compose.vps.yml config --quiet
docker compose -f compose.vps.yml build
docker compose -f compose.vps.yml up -d
docker compose -f compose.vps.yml ps
curl -fsS http://127.0.0.1:8084/health
curl -fsS http://127.0.0.1:8084/ready
```

## Последующие релизы из Git

Не копировать отдельные `.py`-файлы на VPS и не редактировать рабочее дерево
сервера. Сначала локально выполнить тесты, закоммитить и отправить неизменяемый
тег или commit SHA. Для однократной миграции на этот способ скопировать только
скрипт релиза, без `.env`:

```bash
scp deploy/release-from-git.sh hermes@140.82.34.7:/home/hermes/apps/notimate-monitor/deploy/
ssh hermes@140.82.34.7 'chmod 750 /home/hermes/apps/notimate-monitor/deploy/release-from-git.sh'
ssh hermes@140.82.34.7 '/home/hermes/apps/notimate-monitor/deploy/release-from-git.sh <git-tag-or-commit-sha>'
```

Скрипт получает конкретный Git-объект, разворачивает его через `git archive` в
`releases/<commit>`, использует существующий `.env`, пересобирает контейнеры и
переключает `current` только после успешного `/ready`. Старое рабочее дерево не
перезаписывается. Для backup-service после первой миграции:

```bash
sudo install -m 644 deploy/notimate-monitor-backup.service /etc/systemd/system/notimate-monitor-backup.service
sudo systemctl daemon-reload
sudo systemctl restart notimate-monitor-backup.timer
```

## Nginx и TLS

```bash
sudo install -m 644 deploy/nginx-monitor.notimateapp.com.conf /etc/nginx/sites-available/monitor.notimateapp.com
sudo ln -s /etc/nginx/sites-available/monitor.notimateapp.com /etc/nginx/sites-enabled/monitor.notimateapp.com
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d monitor.notimateapp.com
```

## Backup

```bash
chmod 750 deploy/backup-postgres.sh
sudo install -m 644 deploy/notimate-monitor-backup.service /etc/systemd/system/notimate-monitor-backup.service
sudo install -m 644 deploy/notimate-monitor-backup.timer /etc/systemd/system/notimate-monitor-backup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now notimate-monitor-backup.timer
sudo systemctl start notimate-monitor-backup.service
sudo systemctl status notimate-monitor-backup.timer --no-pager
```

## LINE

После успешного `/ready` установить webhook URL `https://monitor.notimateapp.com/webhook`. Webhook redelivery разрешён: запись в PostgreSQL и проекции в Google Sheets идемпотентны по `webhookEventId`.

Постоянное меню отчётов назначается персонально владельцу после заполнения `.env`:

```bash
python3 deploy/configure_owner_rich_menu.py --dry-run
python3 deploy/configure_owner_rich_menu.py
```

Команда не назначает меню по умолчанию всем пользователям: она связывает Rich Menu только с LINE User ID владельцев из `CLIENTS_JSON` и проверяет получившуюся привязку через API.

## Хранение и usage OpenAI

Worker ежедневно по Bangkok-времени очищает raw payload и диагностический текст
завершённых LINE-событий через 14 дней. Неперсональные event ID остаются 90 дней
только для защиты от повторной доставки. Параметры заданы в `.env.vps.example`.

Фактическое потребление OpenAI хранится только в PostgreSQL как дневные агрегаты
`date/model/request count/input/output/reasoning tokens`; сообщения, фото,
финансовые суммы и LINE ID туда не попадают. Отчёт запускается внутри worker:

```bash
docker compose -f compose.vps.yml exec -T worker python deploy/openai_usage_report.py --days 31
```

Полная политика, сроки удаления и DPA-черновик: `../../docs/14 - Pilot Data Processing Schedule.md` и `../../docs/15 - Pilot DPA Draft.md`.
