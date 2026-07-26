-- Idempotent cleanup script for test/placeholder rows that leaked into the
-- production doc index (see live-database audit, T13):
--
--   1. Placeholder sources 's1' / 's2' — leftover test fixtures with no
--      max_pages, 0 pages, enabled = true, status = active. Matched by
--      name, not by hardcoded id (ids are not guaranteed stable across
--      environments).
--   2. A placeholder page 'https://docs.company.com/api-reference' that got
--      indexed under the trusted 'anthropic-api' source — the concerning
--      case, since it is retrievable via search_docs under a real source
--      name. Matched by exact URL, regardless of which source currently
--      owns it.
--   3. Two 'example.com' URLs left behind by the extraction-failure test
--      set. Matched by a URL pattern, not by id.
--
-- Deleting a doc_sources row cascades to doc_pages (ON DELETE CASCADE) and
-- from there to doc_chunks (ON DELETE CASCADE) — see db/init/01_schema.sql.
-- Deleting a doc_pages row directly likewise cascades to its doc_chunks.
--
-- This script is read-only up to the explicit DELETE statements: every
-- DELETE is preceded by a SELECT COUNT(*) reporting exactly what will be
-- removed, so an operator can sanity-check the counts before the delete
-- runs (e.g. by running only the SELECTs first, or reviewing psql output).
-- All predicates are idempotent — re-running this script after the rows are
-- already gone reports 0 affected rows and deletes nothing.
--
-- Usage:
--   psql -v ON_ERROR_STOP=1 -U self_docs -d self_docs -f scripts/cleanup_placeholder_sources.sql
--
-- Wrapped in a single transaction: either everything below commits, or (on
-- any error) nothing does.

BEGIN;

-- 1a. Report: placeholder sources 's1' / 's2' about to be removed, along
--     with the page/chunk rows that will cascade with them.
SELECT
    s.id,
    s.name,
    s.base_url,
    s.enabled,
    s.status,
    (SELECT count(*) FROM doc_pages p WHERE p.source_id = s.id) AS page_count,
    (SELECT count(*)
       FROM doc_chunks c
       JOIN doc_pages p ON p.id = c.page_id
      WHERE p.source_id = s.id) AS chunk_count
FROM doc_sources s
WHERE s.name IN ('s1', 's2');

-- 1b. Delete: cascades to doc_pages -> doc_chunks via FK ON DELETE CASCADE.
DELETE FROM doc_sources
WHERE name IN ('s1', 's2');

-- 2a. Report: the placeholder page indexed under 'anthropic-api' (or any
--     other source it may have been attached to), plus its chunk count.
SELECT
    p.id,
    p.url,
    p.source_id,
    s.name AS source_name,
    (SELECT count(*) FROM doc_chunks c WHERE c.page_id = p.id) AS chunk_count
FROM doc_pages p
JOIN doc_sources s ON s.id = p.source_id
WHERE p.url = 'https://docs.company.com/api-reference';

-- 2b. Delete: cascades to doc_chunks via FK ON DELETE CASCADE. Deletes the
--     page only, not the (legitimate) source it was mixed into.
DELETE FROM doc_pages
WHERE url = 'https://docs.company.com/api-reference';

-- 3a. Report: example.com placeholder URLs from the extraction-failure set.
--     Anchored on the HOST (scheme + optional subdomain + "example.com" +
--     end-of-string or "/"), not an unanchored substring match — `ILIKE
--     '%example.com%'` would also match a legitimate path like
--     `https://real-docs.example.org/example.commands`, since "example.com"
--     is a substring of "example.commands". `~*` (case-insensitive regex)
--     with `^https?://([^/]*\.)?example\.com(/|$)` matches only
--     `example.com` or `<anything>.example.com` as the actual host.
SELECT
    p.id,
    p.url,
    p.source_id,
    s.name AS source_name,
    (SELECT count(*) FROM doc_chunks c WHERE c.page_id = p.id) AS chunk_count
FROM doc_pages p
JOIN doc_sources s ON s.id = p.source_id
WHERE p.url ~* '^https?://([^/]*\.)?example\.com(/|$)';

-- 3b. Delete: cascades to doc_chunks via FK ON DELETE CASCADE.
DELETE FROM doc_pages
WHERE url ~* '^https?://([^/]*\.)?example\.com(/|$)';

COMMIT;
