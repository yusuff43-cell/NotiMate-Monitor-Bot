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

# Document originals (Этап 7, «Бухгалтер»): the photos live on a Docker volume that the SQL
# dump does not include. Best-effort tar from the worker container; a failure here must not
# fail the database backup that already succeeded above.
DOCS_FINAL="$BACKUP_DIR/notimate-documents-$STAMP.tar.gz"
if docker compose -f compose.vps.yml exec -T worker sh -c 'test -d /data/documents && tar -C /data -czf - documents' > "$DOCS_FINAL.tmp" 2>/dev/null; then
  mv "$DOCS_FINAL.tmp" "$DOCS_FINAL"
else
  rm -f "$DOCS_FINAL.tmp"
fi
find "$BACKUP_DIR" -type f -name 'notimate-documents-*.tar.gz' -mtime +14 -delete
