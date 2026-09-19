#!/usr/bin/env bash
# Build and run an immutable release without touching the server's historical worktree.
set -euo pipefail

PROJECT_DIR="${NOTIMATE_PROJECT_DIR:-/home/hermes/apps/notimate-monitor}"
REF="${1:-}"

if [[ -z "$REF" ]]; then
  echo "Usage: $0 <immutable git tag or commit SHA>" >&2
  exit 64
fi

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  echo "Missing $PROJECT_DIR/.env; release aborted." >&2
  exit 1
fi

git -C "$PROJECT_DIR" fetch --tags origin
COMMIT="$(git -C "$PROJECT_DIR" rev-parse --verify "${REF}^{commit}")"
SHORT_COMMIT="$(git -C "$PROJECT_DIR" rev-parse --short=12 "$COMMIT")"
RELEASES_DIR="$PROJECT_DIR/releases"
RELEASE_DIR="$RELEASES_DIR/$SHORT_COMMIT"

mkdir -p "$RELEASES_DIR"
if [[ ! -d "$RELEASE_DIR" ]]; then
  mkdir "$RELEASE_DIR"
  git -C "$PROJECT_DIR" archive "$COMMIT" | tar -x -C "$RELEASE_DIR"
fi

# The release contains no secrets. Docker Compose reads the existing root .env via
# this symlink; the script never prints it or copies it into Git.
ln -sfn "$PROJECT_DIR/.env" "$RELEASE_DIR/.env"

docker compose -f "$RELEASE_DIR/compose.vps.yml" --project-directory "$RELEASE_DIR" build --quiet
docker compose -f "$RELEASE_DIR/compose.vps.yml" --project-directory "$RELEASE_DIR" up -d

for _ in $(seq 1 24); do
  if curl -fsS http://127.0.0.1:8084/ready >/dev/null; then
    ln -sfn "$RELEASE_DIR" "$PROJECT_DIR/current"
    printf '%s\n' "$COMMIT" > "$PROJECT_DIR/current-release.txt"
    echo "Release $SHORT_COMMIT is ready."
    exit 0
  fi
  sleep 5
done

echo "Release $SHORT_COMMIT did not become ready; current symlink was not changed." >&2
exit 1
