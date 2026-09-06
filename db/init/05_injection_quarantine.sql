-- Adds the injection-quarantine review queue: doc_sources.injection_auto_purge
-- (per-source opt-in for silent auto-purge, default FALSE) and doc_quarantine
-- (one row per (url, content_hash) the crawler flagged as a suspected
-- indirect prompt injection). A flagged page's markdown never reaches
-- doc_pages/doc_chunks — this table IS its holding area, reviewed via the
-- admin UI's Allow/Purge actions.
--
-- This single file serves BOTH purposes, same convention as
-- db/init/02_sources_config.sql and db/init/04_upload_sources.sql:
--   1. Fresh volume: db/init/*.sql only runs when the Postgres data
--      directory is empty, so on a brand-new deploy this file runs right
--      after 01_schema.sql/02_.../03_.../04_upload_sources.sql and every
--      statement below is a normal, first-time CREATE/ALTER.
--   2. Live database: db/init/*.sql is SILENTLY SKIPPED once the data
--      directory is non-empty. For an existing deployment this exact file
--      must be applied by hand via `scripts/migrate_injection.sh` (psql).
--      Every statement is idempotent (IF NOT EXISTS / drop-then-add), so
--      this is safe to run again on the fresh-volume path too, and safe to
--      re-run multiple times on a live db.
--
-- Deliberately NOT mirrored into db/init/01_schema.sql.template: this
-- follows the js_render precedent (02_sources_config.sql-only), not the
-- source_type precedent (mirrored into the template AND 04). Touching the
-- template drags in `make configure`'s re-render step and the byte-for-byte
-- parity guard in tests/test_model_registry.py — unnecessary for a column
-- and a table that don't affect the embedding-model schema at all.
--
-- Deliberately NO foreign key from doc_quarantine to doc_pages: `make
-- reindex` runs `TRUNCATE doc_pages, doc_chunks RESTART IDENTITY CASCADE`,
-- and TRUNCATE ... CASCADE also truncates every table with an FK
-- REFERENCING the truncated ones. An FK to doc_pages here would mean a
-- routine re-embed silently destroys every human Allow/Purge decision ever
-- made. There is also no doc_pages row to reference at quarantine time by
-- construction — a URL is either indexed (in doc_pages) or quarantined (in
-- this table), never both for the same content_hash.

ALTER TABLE doc_sources
    ADD COLUMN IF NOT EXISTS injection_auto_purge BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS doc_quarantine (
    id            SERIAL PRIMARY KEY,
    source_id     INT NOT NULL REFERENCES doc_sources(id) ON DELETE CASCADE,
    url           TEXT NOT NULL,
    content_hash  CHAR(64) NOT NULL,     -- hash of the SANITIZED markdown (post Layer-1)
    -- Full content, not an excerpt: a reviewer deciding Allow/Purge must see
    -- exactly what would be indexed, not a truncated snippet next to a URL
    -- they'd otherwise have to open in a browser. NULL once purged — the
    -- tombstone drops the retained payload on purpose (see `state` below).
    markdown      TEXT,
    score         INT NOT NULL DEFAULT 0,
    rule_ids      TEXT[] NOT NULL DEFAULT '{}',
    evidence      TEXT,                  -- truncated excerpts, for the list view
    -- 'quarantined': awaiting human review (or auto-purge disabled).
    -- 'allowed': a human judged this a false positive; indexed immediately,
    --   the decision is pinned to (url, content_hash) so re-syncing
    --   unchanged content never re-flags it.
    -- 'purged': permanently dropped (auto-purge, or a human Purge click).
    --   This is a TOMBSTONE, not a deleted row: deleting the row outright
    --   would make the next sync re-detect the same content and re-queue it
    --   for review forever.
    state         TEXT NOT NULL DEFAULT 'quarantined',
    detected_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),  -- bumped on re-detection, ON CONFLICT
    decided_at    TIMESTAMPTZ,
    decided_by    TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS doc_quarantine_url_hash_idx
    ON doc_quarantine (url, content_hash);            -- the decision-memory key
CREATE INDEX IF NOT EXISTS doc_quarantine_source_state_idx
    ON doc_quarantine (source_id, state);

-- CHECK constraints have no "ADD CONSTRAINT IF NOT EXISTS" form in Postgres.
-- Drop-then-add, matching 04_upload_sources.sql's idiom (test_migration.py
-- bans the DROP/TRUNCATE/DELETE substring specifically from
-- 02_sources_config.sql, not from this file).
ALTER TABLE doc_quarantine DROP CONSTRAINT IF EXISTS doc_quarantine_state_check;
ALTER TABLE doc_quarantine ADD CONSTRAINT doc_quarantine_state_check
    CHECK (state IN ('quarantined', 'allowed', 'purged'));
