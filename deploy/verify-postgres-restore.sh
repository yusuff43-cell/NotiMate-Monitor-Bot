#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/home/hermes/apps/notimate-monitor"
COMPOSE_FILE="$PROJECT_DIR/compose.vps.yml"
BACKUP_FILE="${1:?Pass the absolute path to a PostgreSQL custom-format dump}"
VERIFY_DB="notimate_restore_verify_$(date -u +%Y%m%d%H%M%S)"
CONTAINER_DUMP="/tmp/$(basename "$BACKUP_FILE")"

case "$VERIFY_DB" in
  notimate_restore_verify_*) ;;
  *) echo "Unsafe temporary database name" >&2; exit 1 ;;
esac
test -f "$BACKUP_FILE"

cd "$PROJECT_DIR"
DB_CONTAINER="$(docker compose -f "$COMPOSE_FILE" ps -q db)"
test -n "$DB_CONTAINER"

cleanup() {
  docker compose -f "$COMPOSE_FILE" exec -T db \
    sh -c 'dropdb -U "$POSTGRES_USER" --if-exists "$1"' sh "$VERIFY_DB" >/dev/null 2>&1 || true
  docker exec "$DB_CONTAINER" rm -f "$CONTAINER_DUMP" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker cp "$BACKUP_FILE" "$DB_CONTAINER:$CONTAINER_DUMP"
docker compose -f "$COMPOSE_FILE" exec -T db \
  sh -c 'pg_restore --list "$1" >/dev/null && createdb -U "$POSTGRES_USER" "$2" && pg_restore --exit-on-error -U "$POSTGRES_USER" -d "$2" "$1"' \
  sh "$CONTAINER_DUMP" "$VERIFY_DB"

TABLES="$(docker compose -f "$COMPOSE_FILE" exec -T db \
  sh -c 'psql -U "$POSTGRES_USER" -d "$1" -Atc "SELECT count(*) FROM information_schema.tables WHERE table_schema = '\''public'\''"' \
  sh "$VERIFY_DB")"
EVENTS="$(docker compose -f "$COMPOSE_FILE" exec -T db \
  sh -c 'psql -U "$POSTGRES_USER" -d "$1" -Atc "SELECT count(*) FROM line_events"' \
  sh "$VERIFY_DB")"

test "$TABLES" -ge 1
printf 'backup_restore_verified tables=%s events=%s\n' "$TABLES" "$EVENTS"
