#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/home/hermes/apps/notimate-monitor"
BACKUP_DIR="$PROJECT_DIR/backups"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP_FILE="$BACKUP_DIR/.notimate-monitor-$STAMP.dump.tmp"
FINAL_FILE="$BACKUP_DIR/notimate-monitor-$STAMP.dump"

umask 077
mkdir -p "$BACKUP_DIR"
cd "$PROJECT_DIR"

docker compose -f compose.vps.yml exec -T db \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
  > "$TMP_FILE"

mv "$TMP_FILE" "$FINAL_FILE"
find "$BACKUP_DIR" -type f -name 'notimate-monitor-*.dump' -mtime +14 -delete
