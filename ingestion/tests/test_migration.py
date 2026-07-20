"""Tests for db/init/02_sources_config.sql (the doc_sources crawl-config
migration).

Most of this module asserts things about the migration SQL text itself
(idempotent phrasing, expected columns/constraint) without needing a live
database — these run everywhere, including sandboxes with no Docker.

The one test that actually applies the migration needs a live Postgres
reachable at POSTGRES_* env vars (the compose `db` service, or its test
overlay on 127.0.0.1:5433) and is skipped automatically otherwise — it does
NOT touch the shared `self_docs` database; it creates and drops its own
throwaway database, mirroring the manual verification done for this task.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import psycopg
import pytest

MIGRATION_PATH = Path(__file__).resolve().parents[2] / "db" / "init" / "02_sources_config.sql"

EXPECTED_COLUMNS = [
    "sitemap",
    "include_prefixes",
    "exclude_prefixes",
    "max_pages",
    "language",
    "rate_limit_rps",
    "schedule_cron",
    "enabled",
    "status",
    "proposed_by",
    "created_at",
]


@pytest.fixture(scope="module")
def migration_sql() -> str:
    assert MIGRATION_PATH.is_file(), f"migration file missing: {MIGRATION_PATH}"
    return MIGRATION_PATH.read_text()


def test_migration_file_exists(migration_sql: str) -> None:
    assert migration_sql.strip(), "migration file is empty"


def test_declares_every_expected_column(migration_sql: str) -> None:
    for column in EXPECTED_COLUMNS:
        assert re.search(rf"\bADD COLUMN IF NOT EXISTS\s+{column}\b", migration_sql), (
            f"expected an idempotent 'ADD COLUMN IF NOT EXISTS {column}' in the migration"
        )


def test_column_adds_are_idempotent_by_construction(migration_sql: str) -> None:
    """Every ALTER TABLE ... ADD COLUMN in this file must use IF NOT EXISTS,
    so re-running the file against an already-migrated table is a no-op
    instead of an error."""
    add_column_lines = [
        line
        for line in migration_sql.splitlines()
        if re.search(r"\bADD COLUMN\b", line) and not line.strip().startswith("--")
    ]
    assert add_column_lines, "expected at least one ADD COLUMN statement"
    for line in add_column_lines:
        assert "IF NOT EXISTS" in line, f"non-idempotent ADD COLUMN found: {line!r}"


def test_max_pages_is_nullable_at_sql_layer(migration_sql: str) -> None:
    """max_pages must NOT be NOT NULL here — pydantic's SourceConfig
    (required, gt=0) is what enforces this on write paths; the 9 pre-existing
    doc_sources rows have no backfill value and would break a NOT NULL add."""
    match = re.search(r"ADD COLUMN IF NOT EXISTS\s+max_pages\s+([^,]+)", migration_sql)
    assert match, "max_pages column declaration not found"
    assert "NOT NULL" not in match.group(1).upper()


def test_status_check_constraint_is_guarded(migration_sql: str) -> None:
    """CHECK constraints have no 'ADD CONSTRAINT IF NOT EXISTS' form in
    Postgres, so the add must be wrapped in a DO block that checks
    pg_constraint first, making a second run a no-op instead of an error."""
    assert "doc_sources_status_check" in migration_sql
    assert "CHECK (status IN ('active', 'pending', 'rejected'))" in migration_sql
    assert "pg_constraint" in migration_sql, "constraint add is not guarded against re-run"
    assert re.search(r"DO\s+\$\$", migration_sql), "expected a guarded DO block"


def test_other_add_columns_use_if_exists_or_default_semantics(migration_sql: str) -> None:
    """Sanity check that we didn't accidentally write a DROP/ALTER that would
    be destructive to the 9 live rows — this migration should only ADD."""
    assert "DROP" not in migration_sql.upper()
    assert "TRUNCATE" not in migration_sql.upper()
    assert "DELETE" not in migration_sql.upper()


# --- Live-DB integration test (skipped without Docker/Postgres) -----------

os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("POSTGRES_PORT", "5433")
os.environ.setdefault("POSTGRES_USER", "self_docs")
os.environ.setdefault("POSTGRES_PASSWORD", "testpass123")

_THROWAWAY_DB = "migration_test_pytest"


def _admin_connect():
    return psycopg.connect(
        host=os.environ["POSTGRES_HOST"],
        port=os.environ["POSTGRES_PORT"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname="postgres",
        autocommit=True,
    )


def _db_available() -> bool:
    try:
        conn = _admin_connect()
        conn.close()
        return True
    except psycopg.OperationalError:
        return False


pytestmark_live = pytest.mark.skipif(
    not _db_available(), reason="no live Postgres reachable for migration integration test"
)


@pytestmark_live
def test_migration_applies_idempotently_on_throwaway_db(migration_sql: str) -> None:
    """Builds a throwaway database with today's 01_schema.sql shape, applies
    the migration twice, and asserts the target schema + surviving row data.
    Never touches the shared `self_docs` database."""
    schema_sql = (MIGRATION_PATH.parent / "01_schema.sql").read_text()

    admin = _admin_connect()
    try:
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {_THROWAWAY_DB}")
            cur.execute(f"CREATE DATABASE {_THROWAWAY_DB}")

        conn = psycopg.connect(
            host=os.environ["POSTGRES_HOST"],
            port=os.environ["POSTGRES_PORT"],
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            dbname=_THROWAWAY_DB,
            autocommit=True,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(schema_sql)
                cur.execute(
                    "INSERT INTO doc_sources (name, base_url, last_synced, last_status) "
                    "VALUES ('nextjs', 'https://nextjs.org/docs', now(), 'ok')"
                )

                # Apply twice — must not error the second time.
                cur.execute(migration_sql)
                cur.execute(migration_sql)

                cur.execute(
                    "SELECT sitemap, include_prefixes, exclude_prefixes, max_pages, "
                    "language, rate_limit_rps, schedule_cron, enabled, status, "
                    "proposed_by, name, base_url, last_status "
                    "FROM doc_sources WHERE name = 'nextjs'"
                )
                row = cur.fetchone()
                assert row is not None, "pre-existing row did not survive migration"
                (
                    sitemap,
                    include_prefixes,
                    exclude_prefixes,
                    max_pages,
                    language,
                    rate_limit_rps,
                    schedule_cron,
                    enabled,
                    status,
                    proposed_by,
                    name,
                    base_url,
                    last_status,
                ) = row

                assert name == "nextjs"
                assert base_url == "https://nextjs.org/docs"
                assert last_status == "ok"
                assert sitemap is None
                assert include_prefixes == []
                assert exclude_prefixes == []
                assert max_pages is None
                assert language == "english"
                assert rate_limit_rps == pytest.approx(1.0)
                assert schedule_cron is None
                assert enabled is True
                assert status == "active"
                assert proposed_by is None

                with pytest.raises(psycopg.errors.CheckViolation):
                    cur.execute(
                        "INSERT INTO doc_sources (name, base_url, status) "
                        "VALUES ('bad-source', 'https://example.com', 'bogus')"
                    )
        finally:
            conn.close()
    finally:
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {_THROWAWAY_DB}")
        admin.close()
