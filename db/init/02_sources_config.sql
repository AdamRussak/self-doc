-- Extends doc_sources with the full crawl-config columns (sitemap,
-- include/exclude prefixes, rate limiting, scheduling, and a
-- proposal/approval workflow: status + proposed_by).
--
-- This single file serves BOTH purposes:
--   1. Fresh volume: db/init/*.sql only runs when the Postgres data
--      directory is empty, so on a brand-new deploy this file runs right
--      after 01_schema.sql and the ADD COLUMN statements simply add these
--      columns to the table 01_schema.sql just created.
--   2. Live database: db/init/*.sql is SILENTLY SKIPPED once the data
--      directory is non-empty (Postgres only runs init scripts on first
--      cluster init). For an existing deployment this exact file must be
--      applied by hand via `scripts/migrate.sh` (psql). Every statement
--      below is written to be idempotent — IF NOT EXISTS / IF EXISTS /
--      guarded DO blocks — so this is safe to run again on the fresh-volume
--      path too (init scripts run once, but there is no harm if an operator
--      re-runs 02_sources_config.sql by hand after a fresh init).
--
-- We keep one file instead of two (e.g. a separate "docs/init" copy vs a
-- "migrations/" copy) because the ALTER TABLE ... ADD COLUMN IF NOT EXISTS
-- statements below are valid, no-op-safe SQL regardless of whether the
-- columns already exist (fresh volume, first run) or the table predates
-- them (live db, first run) or they already exist (any second run). Having
-- a second, hand-maintained copy would only invite drift between "what the
-- fresh-volume schema looks like" and "what the live db was migrated to" —
-- exactly the bug class this file exists to avoid.
--
-- max_pages is intentionally left NULLABLE here: the pydantic SourceConfig
-- model (ingestion/app/config.py) is what enforces `max_pages` is required
-- and > 0 on every write path (sync/propose). Enforcing NOT NULL at the SQL
-- layer would break the 9 existing rows that predate this column with no
-- backfill value to give them. This split (permissive schema + strict
-- application-layer validation) is deliberate — do not "fix" it by adding a
-- NOT NULL constraint here.

ALTER TABLE doc_sources
    ADD COLUMN IF NOT EXISTS sitemap          TEXT,
    ADD COLUMN IF NOT EXISTS include_prefixes TEXT[]      NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS exclude_prefixes TEXT[]      NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS max_pages        INT,
    ADD COLUMN IF NOT EXISTS language         TEXT        NOT NULL DEFAULT 'english',
    ADD COLUMN IF NOT EXISTS rate_limit_rps   REAL        NOT NULL DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS schedule_cron    TEXT,
    ADD COLUMN IF NOT EXISTS enabled          BOOLEAN     NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS status           TEXT        NOT NULL DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS proposed_by      TEXT,
    ADD COLUMN IF NOT EXISTS created_at       TIMESTAMPTZ NOT NULL DEFAULT now();

-- CHECK constraints have no "ADD CONSTRAINT IF NOT EXISTS" form in Postgres,
-- so guard the add with an explicit pg_constraint lookup to make re-running
-- this file safe.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'doc_sources_status_check'
          AND conrelid = 'doc_sources'::regclass
    ) THEN
        ALTER TABLE doc_sources
            ADD CONSTRAINT doc_sources_status_check
            CHECK (status IN ('active', 'pending', 'rejected'));
    END IF;
END;
$$;
