#!/usr/bin/env bash
# Applies db/init/03_fix_embedding_dim.sql to a running self-docs Postgres
# container to update canonical URLs, sitemaps, and include_prefixes for failed sources.

set -euo pipefail

CONTAINER="${CONTAINER:-self-docs-db}"
POSTGRES_USER="${POSTGRES_USER:-self_docs}"
POSTGRES_DB="${POSTGRES_DB:-self_docs}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIGRATION_FILE="${SCRIPT_DIR}/../db/init/03_fix_embedding_dim.sql"

if [[ ! -f "${MIGRATION_FILE}" ]]; then
    echo "migrate_fixes.sh: migration file not found: ${MIGRATION_FILE}" >&2
    exit 1
fi

echo "migrate_fixes.sh: applying ${MIGRATION_FILE} to database '${POSTGRES_DB}' in container '${CONTAINER}'..."

docker exec -i "${CONTAINER}" psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" < "${MIGRATION_FILE}"

echo "migrate_fixes.sh: done."
