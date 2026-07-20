# ADR-003: llms.txt Fast Path, HTTP Conditional Skip, and Per-Source Multilingual FTS

**Status:** Accepted
**Date:** 2026-07-20
**Decision makers:** Project owner + architect (pre-dispatch review)

## Context

Three related crawl-efficiency and retrieval-quality gaps surfaced once the
seed sources (`fastapi`, `nextjs`, `pgvector-readme`, `traefik`, ...) were
running on a weekly schedule against the home-lab's fixed resource budget
(shared 1.5G RAM across `db` + `ingestion` + `mcp-server`):

1. **Crawl cost for sites that already publish an [llms.txt](https://llmstxt.org)
   index.** A growing number of documentation sites (framework docs,
   API references) now ship a pre-cleaned `/llms.txt` or `/llms-full.txt`
   markdown export specifically for LLM consumption. Our BFS/sitemap HTML
   crawler was ignoring these and re-deriving the same content the slow way —
   fetch every HTML page, strip boilerplate, extract markdown — burning CPU
   and wall-clock time (and, for JS-rendered doc sites, sometimes extracting
   nothing useful at all, since our crawler does not execute JavaScript).
2. **Redundant re-fetch on every re-sync.** Weekly (or cron-scheduled)
   re-syncs re-downloaded and re-extracted every page of every source even
   when nothing had changed upstream, because the only change-detection we
   had was a post-download content-hash diff (`pages_skipped`) — the network
   fetch and markdown extraction still happened before that hash comparison.
3. **Single hardcoded FTS language.** `doc_chunks.fts` was a generated column
   hardcoded to `to_tsvector('english', content)`. The admin UI already
   exposed a `language` field per source (from the original `sources.yaml`
   schema), but it was silently ignored at the SQL layer — a non-English doc
   source got English-stemmed indexing regardless of what an operator
   configured.

## Decision

1. **`llms_txt` fast path (`ingestion/app/llms_txt.py` + a new branch in
   `crawler.py`).** Each source gets a new `llms_txt` field, one of
   `auto | off | only` (default `'auto'`, `doc_sources_llms_txt_check`
   constraint in `db/init/02_sources_config.sql`):
   - `auto` — try `{origin}/llms-full.txt`, then `{origin}/llms.txt`. If
     either is found, parse it (H1 title / optional blockquote summary / H2
     or H1 section splits) directly into pre-cleaned markdown pages and skip
     the HTML crawl entirely for this sync. If neither exists (or fetch
     fails), fall back to the existing sitemap/BFS HTML crawl unchanged.
   - `off` — always use the HTML crawl (previous, only behavior).
   - `only` — use the llms index if found; index nothing for this source if
     it is not found (no HTML fallback).
2. **HTTP conditional skip (ETag / If-Modified-Since).** `doc_pages` gained
   `etag`/`last_modified` columns. On every re-sync, the crawler sends
   `If-None-Match`/`If-Modified-Since` for any page with previously-recorded
   validators; a `304 Not Modified` response short-circuits before download
   or markdown extraction and is counted separately from `pages_skipped` as
   `pages_not_modified` (new Prometheus counter `pages_not_modified_total`,
   labeled by source). `doc_sources` also gained `llms_etag`/
   `llms_last_modified` for the same conditional-GET treatment of the whole
   llms index file — the read/write plumbing is in place; surfacing a
   304-short-circuit for the index fetch itself (skipping the parse step
   too, not just the per-page fetches) is left as a follow-up.
3. **Per-source multilingual full-text search.** `doc_chunks` gained
   `fts_config regconfig NOT NULL DEFAULT 'english'`, and the generated `fts`
   column is redefined as `to_tsvector(fts_config, content)` (was hardcoded
   `to_tsvector('english', content)`). Ingestion stamps each inserted chunk's
   `fts_config` from `source.language`; retrieval queries use
   `websearch_to_tsquery(dc.fts_config, ...)` against the matching chunk
   instead of a fixed `'english'`. `SourceConfig.language` is now validated
   against a 30-name allowlist of Postgres built-in text-search
   configurations (`SUPPORTED_FTS_LANGUAGES` in `ingestion/app/config.py`) at
   config-load time, instead of failing silently or erroring only at query
   time on an invalid name. This is what makes the admin UI's existing
   `language` field actually take effect.

Per ADR-002 (nuke-and-rebuild), both init scripts were updated together:
`db/init/01_schema.sql` (fresh volume — `fts_config`, the redefined `fts`
generated column, `doc_pages.etag`/`last_modified`, and the `doc_sources`
`llms_txt`/`llms_etag`/`llms_last_modified` columns are all present from
first init) and `db/init/02_sources_config.sql` (idempotent `ALTER TABLE ...
ADD COLUMN IF NOT EXISTS` statements plus a guarded `DO` block, for
`scripts/migrate.sh` to apply against an existing live database without a
volume wipe). `.github/workflows/test.yml` now applies both SQL files when
building the CI database, so CI exercises the same live-migration path as
production.

## Rationale

**Why llms.txt over improving the HTML crawler further.** llms.txt is
authored by the upstream site specifically to be clean, complete, ad/nav-free
markdown — strictly higher quality input than anything our own boilerplate
stripper can derive from rendered HTML, at a fraction of the request count
(1-2 requests vs. up to `max_pages`). `auto` mode makes this free to adopt:
any source without an llms.txt file behaves exactly as before.

**Why not a headless browser (Playwright/Puppeteer) for JS-rendered doc
sites.** A subset of documentation sites are JS SPAs that our `httpx`-based
crawler cannot fully render, and a headless-browser crawl step was
considered as the general fix. Rejected for this deployment:
- **RAM budget.** The whole stack (`db` + `ingestion` + `mcp-server`) is
  sized against the home-lab's 1.5G budget. A headless Chromium instance
  alone routinely costs several hundred MB resident, before accounting for
  concurrent page contexts during a crawl — that is a large fraction of the
  entire budget for one crawler dependency.
- **Image/binary bloat.** Playwright/Puppeteer pull a full browser binary
  into the `ingestion` image (hundreds of MB), working against the same
  "small, auditable image" property the rest of the stack optimizes for
  (see ADR-001's embedding-model choice for the same reasoning applied to
  FastEmbed vs. a heavier inference server).
- **llms.txt solves the actual problem more cheaply for the sites that
  matter.** The JS-SPA doc sites in practice are exactly the kind of
  well-maintained framework/product docs that are also most likely to
  publish an `llms.txt` (it is the same "make this site legible to an LLM
  agent" motivation). `auto` mode captures that overlap directly, without
  paying the RAM/binary cost of a renderer for the sites that don't
  overlap. A sitemap-less, llms.txt-less, JS-rendered site remains
  under-indexed after this change — recorded as a known gap, not solved by
  this ADR — and would be revisited only if it becomes the dominant case
  rather than the exception.

**Why HTTP conditional GET instead of relying solely on the existing
content-hash skip.** The post-fetch hash comparison (`pages_skipped`)
already avoided redundant re-*indexing*, but it did nothing for redundant
re-*fetching* — every re-sync still downloaded and markdown-extracted every
page before discovering the content was unchanged. `ETag`/
`If-Modified-Since` is standard HTTP, requires no new dependency, and moves
the "nothing changed" detection to before the expensive work instead of
after it. `pages_not_modified` is tracked as a distinct counter from
`pages_skipped` (hash-diff) rather than folded into it, since they represent
different mechanisms and different sources reporting different validator
support are worth distinguishing operationally.

**Why a 30-name allowlist instead of accepting any string.** Passing an
unrecognized configuration name straight to `to_tsvector`/
`websearch_to_tsquery` fails at query time inside Postgres with an opaque
error, not at config-save time in the admin UI/`propose_doc_source`
validation. Validating against the actual list of Postgres built-in
text-search configurations up front gives an operator (or a proposing
agent) an immediate, actionable error instead of a source that silently
fails to sync (or fails at search time) weeks later.

## Consequences

- **Positive:** Sources with an available llms.txt index sync dramatically
  faster and with cleaner extracted content, at effectively zero added risk
  for sources without one (`auto` falls straight back to the prior crawl
  path).
- **Positive:** Unchanged-page re-syncs on validator-supporting origins skip
  the network fetch and extraction step entirely, not just the DB write —
  faster weekly re-syncs, less outbound bandwidth against origins we don't
  control.
- **Positive:** Non-English documentation sources now get correctly stemmed
  full-text search instead of silently being treated as English, and an
  invalid `language` value is caught at config time instead of at query
  time.
- **Negative — mode-switch re-index churn.** Changing a source's `llms_txt`
  mode (e.g. `off` → `auto` after the upstream site adds an llms.txt file)
  changes which URLs get indexed for that source and triggers a re-index on
  the next sync — old HTML-derived pages are replaced by llms.txt-derived
  pages (or vice versa) under the existing purge-ratio/coverage guards. This
  is expected churn, not a bug, and the existing guards (which already
  refuse a sync that would delete an implausibly large fraction of a
  source's pages) bound the blast radius; it is not catastrophic, but an
  operator switching modes on a large existing source should expect a full
  re-crawl of that source, not an incremental diff.
- **Negative — live-migration cost for the `fts_config` change.** On a
  database created before this change, `doc_chunks.fts` is redefined from a
  hardcoded `to_tsvector('english', content)` expression to
  `to_tsvector(fts_config, content)`. Postgres has no `ALTER COLUMN ...`
  form for a generated column's expression, so `02_sources_config.sql` drops
  and re-adds the `fts` column, which **rewrites the entire `doc_chunks`
  table and rebuilds the GIN index** (`doc_chunks_fts_idx`). This is a
  locking, non-instant operation proportional to corpus size — see the
  runbook for the maintenance-window guidance. On a fresh volume
  (nuke-and-rebuild, ADR-002's path) this cost does not apply: `01_schema.sql`
  creates the column correctly from the start.
- **Negative:** `llms_txt.py` and the crawler's llms-index branch are new
  surface area to maintain (parsing `/llms.txt`'s H1/blockquote/H2 structure,
  handling malformed or partial index files) alongside the existing HTML
  extraction path, rather than one crawl path to reason about.

## Related

- `ingestion/app/llms_txt.py`, `ingestion/app/crawler.py` — llms.txt
  discovery/parsing and the crawl-mode branch
- `ingestion/app/store.py` — conditional-GET validator plumbing, chunk
  `fts_config` stamping, `pages_not_modified` counter
- `ingestion/app/config.py` — `SUPPORTED_FTS_LANGUAGES`, `SourceConfig.llms_txt`
- `db/init/01_schema.sql`, `db/init/02_sources_config.sql` — schema for all
  three features (fresh-volume and live-migration paths)
- ADR-001 — the RAM/binary-footprint reasoning this ADR's "why not a headless
  browser" section reuses
- ADR-002 — the nuke-and-rebuild vs. live-migrate strategy this ADR follows
  for both `01_schema.sql` and `02_sources_config.sql`
- `docs/runbook.md` §§ "Add a new doc source", "REQUIRED — apply the
  `doc_sources` config migration" — operational detail for the new fields,
  the migration caveat, and the new metric
