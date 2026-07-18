#!/usr/bin/env bash
# self-docs automated backup script.
# Runs `make backup-auto` (backup + prune) in the repo directory.
# Designed to be called by systemd timer, cron, or manually.
#
# Usage:
#   ./scripts/backup.sh                   # uses defaults
#   KEEP=7 ./scripts/backup.sh            # keep 7 most recent backups
#   SELF_DOCS_DIR=/opt/self-docs ./scripts/backup.sh  # custom repo path
#
# Exit codes:
#   0 — backup succeeded
#   1 — backup failed (compose not running, db unreachable, etc.)

set -euo pipefail

SELF_DOCS_DIR="${SELF_DOCS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
KEEP="${KEEP:-4}"

cd "$SELF_DOCS_DIR"

echo "[$(date -Iseconds)] self-docs backup starting (dir=$SELF_DOCS_DIR, keep=$KEEP)"

if ! docker compose ps db --status running -q 2>/dev/null | grep -q .; then
    echo "ERROR: db container is not running. Start with: docker compose up -d db" >&2
    exit 1
fi

make backup-auto KEEP="$KEEP"

echo "[$(date -Iseconds)] self-docs backup complete."
