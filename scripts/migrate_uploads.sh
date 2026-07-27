#!/usr/bin/env bash
# Applies db/init/04_upload_sources.sql to a running self-docs Postgres
# container. Safe to run more than once — every statement in that file is
# idempotent (ADD COLUMN IF NOT EXISTS / DROP CONSTRAINT IF EXISTS followed
# by ADD CONSTRAINT).
#
# Reads the same env vars the compose services use (POSTGRES_USER,
# POSTGRES_DB, ...). Source your .env first, e.g.:
#
#   set -a; source .env; set +a; ./scripts/migrate_uploads.sh
#
# Or pass them inline:
#
#   POSTGRES_USER=self_docs POSTGRES_DB=self_docs ./scripts/migrate_uploads.sh
#
# By default this targets the `self-docs-db` container name used by
# docker-compose.yml. Override with CONTAINER=<name> if needed.

set -euo pipefail

CONTAINER="${CONTAINER:-self-docs-db}"
POSTGRES_USER="${POSTGRES_USER:?POSTGRES_USER must be set}"
POSTGRES_DB="${POSTGRES_DB:?POSTGRES_DB must be set}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIGRATION_FILE="${SCRIPT_DIR}/../db/init/04_upload_sources.sql"

if [[ ! -f "${MIGRATION_FILE}" ]]; then
    echo "migrate_uploads.sh: migration file not found: ${MIGRATION_FILE}" >&2
    exit 1
fi

echo "migrate_uploads.sh: applying ${MIGRATION_FILE} to database '${POSTGRES_DB}' in container '${CONTAINER}' as '${POSTGRES_USER}'..."

docker exec -i "${CONTAINER}" psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" < "${MIGRATION_FILE}"

echo "migrate_uploads.sh: done."
