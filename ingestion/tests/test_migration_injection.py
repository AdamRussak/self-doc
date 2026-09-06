"""Tests for db/init/05_injection_quarantine.sql (the injection-quarantine
migration).

Most of this module asserts things about the migration SQL text itself
(idempotent phrasing, expected columns/constraints, the deliberate absence
of a foreign key to doc_pages) without needing a live database — these run
everywhere, including sandboxes with no Docker.

The one test that actually applies the migration needs a live Postgres
reachable at POSTGRES_* env vars (the compose `db` service, or its test
overlay on 127.0.0.1:5433) and is skipped automatically otherwise — it does
NOT touch the shared `self_docs` database; it creates and drops its own
throwaway database, mirroring test_migration.py's approach for
02_sources_config.sql.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import psycopg
import pytest

MIGRATION_PATH = Path(__file__).resolve().parents[2] / "db" / "init" / "05_injection_quarantine.sql"

EXPECTED_QUARANTINE_COLUMNS = [
    "id", "source_id", "url", "content_hash", "markdown", "score",
    "rule_ids", "evidence", "state", "detected_at", "last_seen_at",
    "decided_at", "decided_by",
]


@pytest.fixture(scope="module")
def migration_sql() -> str:
    assert MIGRATION_PATH.is_file(), f"migration file missing: {MIGRATION_PATH}"
    return MIGRATION_PATH.read_text()


def test_migration_file_exists(migration_sql: str) -> None:
    assert migration_sql.strip(), "migration file is empty"


def test_declares_injection_auto_purge_column(migration_sql: str) -> None:
    assert re.search(r"\bADD COLUMN IF NOT EXISTS\s+injection_auto_purge\s+BOOLEAN\s+NOT\s+NULL\s+DEFAULT\s+FALSE\b", migration_sql, re.I), (
        "injection_auto_purge must default FALSE — upgrading an existing "
        "deployment must never silently start auto-purging pages"
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


def test_quarantine_table_created_idempotently(migration_sql: str) -> None:
    assert re.search(r"\bCREATE TABLE IF NOT EXISTS\s+doc_quarantine\b", migration_sql), (
        "doc_quarantine must be created with IF NOT EXISTS"
    )


def test_quarantine_table_declares_every_expected_column(migration_sql: str) -> None:
    create_stmt_match = re.search(r"CREATE TABLE IF NOT EXISTS doc_quarantine \((.*?)\n\);", migration_sql, re.S)
    assert create_stmt_match, "could not locate the doc_quarantine CREATE TABLE body"
    body = create_stmt_match.group(1)
    for column in EXPECTED_QUARANTINE_COLUMNS:
        assert re.search(rf"^\s*{column}\b", body, re.M), f"expected column {column!r} in doc_quarantine"


def test_quarantine_has_no_foreign_key_to_doc_pages(migration_sql: str) -> None:
    """The load-bearing correctness fix: `make reindex` runs
    `TRUNCATE doc_pages, doc_chunks RESTART IDENTITY CASCADE`, and CASCADE
    also truncates every table with an FK REFERENCING the truncated ones.
    A foreign key from doc_quarantine to doc_pages would mean a routine
    re-embed silently destroys every human Allow/Purge decision ever made.
    There is also no doc_pages row to reference at quarantine time by
    construction, so this must never be (re-)added."""
    assert "REFERENCES doc_pages" not in migration_sql


def test_quarantine_references_doc_sources_with_cascade(migration_sql: str) -> None:
    assert re.search(r"REFERENCES\s+doc_sources\(id\)\s+ON\s+DELETE\s+CASCADE", migration_sql), (
        "doc_quarantine rows for a deleted source must be cleaned up automatically"
    )


def test_url_content_hash_is_unique(migration_sql: str) -> None:
    """(url, content_hash) is the decision-memory key — without a unique
    index, record_quarantine's ON CONFLICT upsert has nothing to conflict
    on and every re-detection duplicates a row instead of updating one."""
    assert re.search(r"CREATE UNIQUE INDEX IF NOT EXISTS\s+doc_quarantine_url_hash_idx\s+ON\s+doc_quarantine\s+\(url,\s*content_hash\)", migration_sql)


def test_state_check_constraint_is_guarded(migration_sql: str) -> None:
    """CHECK constraints have no 'ADD CONSTRAINT IF NOT EXISTS' form in
    Postgres, so the add must be drop-then-add (matching
    04_upload_sources.sql's idiom) to stay re-runnable."""
    assert "doc_quarantine_state_check" in migration_sql
    assert "CHECK (state IN ('quarantined', 'allowed', 'purged'))" in migration_sql
    assert "DROP CONSTRAINT IF EXISTS doc_quarantine_state_check" in migration_sql


def test_file_contains_no_destructive_statements(migration_sql: str) -> None:
    """Sanity check that this migration only ever ADDs — no accidental
    DROP/TRUNCATE/DELETE *statement* that would be destructive to existing
    data. Two expected exceptions, both normal parts of a CREATE TABLE
    definition rather than a standalone destructive statement: DROP
    CONSTRAINT IF EXISTS (matching 04_upload_sources.sql's drop-then-add
    CHECK idiom) and ON DELETE CASCADE (the FK clause every table in
    01_schema.sql already uses)."""
    # Strip both whole-line comments and inline trailing `-- ...` comments
    # before checking, and match whole words only — a column comment like
    # "-- truncated excerpts" would otherwise false-positive on TRUNCATE via
    # plain substring matching (TRUNCATE is a substring of TRUNCATED).
    code_only_lines = [line.split("--", 1)[0] for line in migration_sql.splitlines()]
    non_constraint_lines = [
        line
        for line in code_only_lines
        if re.search(r"\bDROP\b", line, re.I) and not re.search(r"\bDROP\s+CONSTRAINT\s+IF\s+EXISTS\b", line, re.I)
    ]
    assert not non_constraint_lines, f"unexpected DROP statement(s): {non_constraint_lines}"
    assert not any(re.search(r"\bTRUNCATE\b", line, re.I) for line in code_only_lines)
    non_fk_delete_lines = [
        line
        for line in code_only_lines
        if re.search(r"\bDELETE\b", line, re.I) and not re.search(r"\bON\s+DELETE\s+CASCADE\b", line, re.I)
    ]
    assert not non_fk_delete_lines, f"unexpected DELETE statement(s): {non_fk_delete_lines}"


# --- Live-DB integration test (skipped without Docker/Postgres) -----------

os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("POSTGRES_PORT", "5433")
os.environ.setdefault("POSTGRES_USER", "self_docs")
os.environ.setdefault("POSTGRES_PASSWORD", "testpass123")

_THROWAWAY_DB = "injection_migration_test_pytest"


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
    """Builds a throwaway database with today's full schema (01 -> 02 -> 04),
    applies this migration twice, and asserts the target shape + that
    TRUNCATE-cascade behaviour matches the design intent. Never touches the
    shared `self_docs` database."""
    init_dir = MIGRATION_PATH.parent
    schema_sql = (init_dir / "01_schema.sql").read_text()
    sources_config_sql = (init_dir / "02_sources_config.sql").read_text()
    uploads_sql = (init_dir / "04_upload_sources.sql").read_text()

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
                cur.execute(sources_config_sql)
                cur.execute(uploads_sql)
                cur.execute(
                    "INSERT INTO doc_sources (name, base_url, last_synced, last_status) "
                    "VALUES ('nextjs', 'https://nextjs.org/docs', now(), 'ok') RETURNING id"
                )
                source_id = cur.fetchone()[0]

                # Apply twice — must not error the second time.
                cur.execute(migration_sql)
                cur.execute(migration_sql)

                cur.execute("SELECT injection_auto_purge FROM doc_sources WHERE id = %s", (source_id,))
                assert cur.fetchone()[0] is False

                cur.execute(
                    "INSERT INTO doc_quarantine (source_id, url, content_hash, markdown, score, rule_ids) "
                    "VALUES (%s, 'https://nextjs.org/docs/evil', %s, 'poison', 150, ARRAY['override_ignore_prior'])",
                    (source_id, "a" * 64),
                )

                with pytest.raises(psycopg.errors.CheckViolation):
                    cur.execute(
                        "INSERT INTO doc_quarantine (source_id, url, content_hash, state) "
                        "VALUES (%s, 'https://nextjs.org/docs/x', %s, 'bogus')",
                        (source_id, "b" * 64),
                    )
        finally:
            conn.close()

        # make reindex's TRUNCATE ... CASCADE must NOT wipe doc_quarantine —
        # the whole point of having no FK to doc_pages/doc_chunks.
        conn2 = psycopg.connect(
            host=os.environ["POSTGRES_HOST"], port=os.environ["POSTGRES_PORT"],
            user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"],
            dbname=_THROWAWAY_DB, autocommit=True,
        )
        try:
            with conn2.cursor() as cur:
                cur.execute("TRUNCATE doc_pages, doc_chunks RESTART IDENTITY CASCADE")
                cur.execute("SELECT count(*) FROM doc_quarantine")
                assert cur.fetchone()[0] == 1, (
                    "make reindex's TRUNCATE ... CASCADE destroyed a quarantine "
                    "decision — this is the exact bug the missing FK to doc_pages "
                    "is supposed to prevent"
                )
        finally:
            conn2.close()
    finally:
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {_THROWAWAY_DB}")
        admin.close()
