# Запуск Monitor Bot на VPS

Целевая папка: `/home/hermes/apps/notimate-monitor`.

Состав: PostgreSQL 16, web на `127.0.0.1:8084`, отдельный worker, Nginx и ежедневный PostgreSQL backup.

## Секреты

Скопировать `.env.vps.example` в `.env`, заполнить реальные значения и установить права `600`. Для `POSTGRES_PASSWORD` использовать URI-безопасную hex-строку:

```bash
openssl rand -hex 32
```

Не отправлять `.env` в Git или чат.

## Проверка конфигурации и запуск

```bash
docker compose -f compose.vps.yml config --quiet
docker compose -f compose.vps.yml build
docker compose -f compose.vps.yml up -d
docker compose -f compose.vps.yml ps
curl -fsS http://127.0.0.1:8084/health
curl -fsS http://127.0.0.1:8084/ready
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

После успешного `/ready` установить webhook URL `https://monitor.notimateapp.com/webhook`. До проверки идемпотентности Google Sheets не включать Webhook redelivery.

Постоянное меню отчётов назначается персонально владельцу после заполнения `.env`:

```bash
python3 deploy/configure_owner_rich_menu.py --dry-run
python3 deploy/configure_owner_rich_menu.py
```

Команда не назначает меню по умолчанию всем пользователям: она связывает Rich Menu только с LINE User ID владельцев из `CLIENTS_JSON` и проверяет получившуюся привязку через API.
