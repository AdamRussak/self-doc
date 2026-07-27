-- Adds source_type to doc_sources so uploaded documents (Markdown/text,
-- HTML, PDF, zip bundles) can live alongside crawled sources in the same
-- table. 'crawl' (the default) means base_url is a live site fetched by the
-- ingestion pipeline, as it always has been. 'upload' means base_url holds
-- an 'upload://{name}/{rel_path}' sentinel URL in place of a real crawl
-- target — the row's doc_pages/doc_chunks were populated by parsing an
-- uploaded file through the existing chunker/embedder pipeline. Raw
-- uploaded bytes are never persisted; only the parsed markdown is stored.
--
-- This single file serves BOTH purposes, same convention as
-- db/init/02_sources_config.sql and db/init/03_fix_embedding_dim.sql:
--   1. Fresh volume: db/init/*.sql only runs when the Postgres data
--      directory is empty, so on a brand-new deploy this file runs right
--      after 01_schema.sql/02_sources_config.sql/03_fix_embedding_dim.sql
--      and the ADD COLUMN statement is a no-op (01_schema.sql.template
--      already defines source_type on fresh installs).
--   2. Live database: db/init/*.sql is SILENTLY SKIPPED once the data
--      directory is non-empty (Postgres only runs init scripts on first
--      cluster init). For an existing deployment this exact file must be
--      applied by hand via `scripts/migrate_uploads.sh` (psql). Every
--      statement below is written to be idempotent — IF NOT EXISTS /
--      IF EXISTS / guarded constraint drop-then-add — so this is safe to
--      run again on the fresh-volume path too, and safe to re-run multiple
--      times on a live db.

ALTER TABLE doc_sources
    ADD COLUMN IF NOT EXISTS source_type TEXT NOT NULL DEFAULT 'crawl';

-- CHECK constraints have no "ADD CONSTRAINT IF NOT EXISTS" form in Postgres.
-- Drop-then-add (rather than the pg_constraint-lookup guard used in
-- 03_fix_embedding_dim.sql) keeps this re-runnable and also lets the
-- constraint definition itself change here later without needing a new
-- migration file to fix a stale check.
ALTER TABLE doc_sources
    DROP CONSTRAINT IF EXISTS doc_sources_source_type_check;

ALTER TABLE doc_sources
    ADD CONSTRAINT doc_sources_source_type_check
    CHECK (source_type IN ('crawl', 'upload'));
