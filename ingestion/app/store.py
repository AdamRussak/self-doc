"""Hash-diff sync orchestration: crawl -> extract -> chunk -> embed -> upsert.

Drift handling (per IMPLEMENTATION_PLAN.md §2 "Drift handling" and the T4
task description):

  - Page-level SHA-256 hash of the *extracted markdown* (not raw HTML) is the
    unit of change detection.
  - `doc_sources` gets a row per configured source (created on first sync,
    `base_url` kept in sync on later ones).
  - Pages whose hash is unchanged are skipped entirely (no re-embed, no
    write).
  - New/changed pages are replaced in ONE transaction per page: `DELETE FROM
    doc_pages WHERE url = ...` (cascades to `doc_chunks`), then reinsert the
    page row and its chunks/embeddings.
  - After a source's crawl completes, any `doc_pages` row for that source
    whose URL was not seen in this crawl is deleted (page removed upstream).
    Pages yielded with `fetch_ok=False` (attempted fetch failures like 503s or
    robots disallow) ARE added to `seen_urls`, protecting their existing rows
    from being purged while recording a page failure.
  - `doc_sources.last_synced`/`last_status` are updated at the end of every
    sync attempt. `last_status` is `ok` (no page failures), `partial` (some
    pages failed but at least one succeeded/was unchanged), or `failed`
    (source-level failure, e.g. the crawl itself raised — dead sitemap host,
    etc.).
  - When `crawler.crawl` reports its sitemap discovery was truncated at
    `max_pages` (a `crawl_summary` sentinel item with `truncated_at_cap:
    True` — see `crawler.crawl`'s docstring), `seen_urls` is a real but
    INCOMPLETE enumeration of the source. Deleting the "missing" pages is
    therefore unsafe: absence from an incomplete enumeration is not evidence
    of upstream removal, only of not having been sampled this run. This
    sync's `_delete_missing_pages` step is skipped entirely (same treatment
    as `crawl_aborted_early`) and the sync classifies as `partial` via
    `classify_sync`'s `crawl_truncated` parameter. Fixes the 2026 sync-drift
    incident where two sources (`gemini-api`, `google-search-console-api`)
    fetched exactly as many pages as they deleted on every run, because
    sitemap enumeration order is not stable and each run sampled a different
    arbitrary slice under the cap. (`crawler.discover_sitemap_urls` now also
    sorts URLs before applying the cap so repeated runs over an unchanged
    sitemap select the identical slice, independent of this guard.)
"""

from __future__ import annotations

import hashlib
import inspect
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import psycopg

from . import chunker, crawler, embedder, extract, metrics, renderer
from .config import SourceConfig
from .logging_config import get_logger
from .sources_repo import SourceRecord
from .uploads import UploadedDoc

logger = get_logger(component="store")

# --- Purge-ratio guard (defense in depth for _delete_missing_pages) --------------------
#
# A completed crawl's `seen_urls` is not always a trustworthy FULL
# enumeration of a source, even though it's non-empty: silent BFS fallback
# (sitemap fetch fails -> link-graph BFS from base_url, which routinely
# reaches a fraction of the sitemap's coverage) and sitemap `max_pages`
# truncation both complete normally while under-reporting. Naively trusting
# any non-empty `seen_urls` and deleting everything else can gut a source.
#
# A raw "refuse if we'd delete more than X% of the source" ratio is too
# blunt on its own: a *deliberate* corpus repair (e.g. narrowing an
# `include_prefixes` filter to drop pages that were always out of scope) can
# legitimately delete a similarly large fraction. The two need a second
# signal to tell apart. The distinguishing signature:
#   - a broken enumeration (BFS collapse, cap truncation) fetches FEW pages
#     and would delete MANY: low coverage, high deletion.
#   - a deliberate repair fetches MANY pages (a healthy, comparably-sized
#     re-crawl) while also deleting many: high coverage, high deletion.
# So the guard only refuses when a large deletion is paired with LOW crawl
# coverage of the existing corpus size — high coverage justifies a large
# deletion regardless of the ratio.
#
# Calibrated against the traefik repair (T12/R1 incident) with real,
# measured numbers (see docstring on `_delete_missing_pages`):
#   - intended repair:  397 existing, ~280 fetched (coverage 0.705), ~232
#     purged (delete ratio 0.584)         -> must be PERMITTED
#   - BFS-collapse scenario: 397 existing, ~60 fetched (coverage 0.151),
#     ~337 purged (delete ratio 0.849)    -> must be REFUSED
# 0.5 / 0.3 sits with comfortable margin on both signals for both cases (the
# repair's 0.705 coverage clears the 0.3 floor by 0.4; the collapse's 0.151
# coverage misses it by 0.15) — see `_delete_missing_pages` for the exact
# comparison.
PURGE_DELETE_RATIO_THRESHOLD = 0.5
PURGE_FETCH_COVERAGE_FLOOR = 0.3

# Below this many existing pages, the ratio guard doesn't engage at all —
# a handful of pages moving by one or two is a 50%+ ratio on paper but not a
# meaningful signal of a broken enumeration, and real sources this small
# (tests, brand-new sources) shouldn't be hamstrung by it. The unconditional
# empty-`seen_urls` guard below still applies regardless of size.
PURGE_RATIO_GUARD_MIN_EXISTING_PAGES = 20

# --- Sync status classification --------------------------------------------
#
# 2026-07-26 sync-health incident: a 40-source production run reported
# `last_status="ok"` for every single source — including 6 that indexed zero
# pages and traefik, which silently lost 117/280 pages (42%) to soft
# failures. Root cause was an implicit conditional chain where
# `pages_soft_failed` counted toward both `pages_seen` (defeating the
# "empty crawl" failed-guard) and `succeeded_any` (making any nonzero
# soft-failure count look like a "success" as long as the HARD-failure
# counter `pages_failed` was zero). `classify_sync` below replaces that
# chain with an explicit, directly unit-testable set of ordered rules — see
# its docstring for the exact rule order.
#
# Ratio above which soft failures alone (zero hard failures) demote an
# otherwise-clean sync from "ok" to "partial". Calibrated against the
# traefik incident: 117/280 = 0.418 soft-failure ratio, comfortably above
# this floor -> must be "partial". A source with only a couple of
# incidental soft failures out of hundreds of pages shouldn't flap to
# "partial" on every run, hence a floor rather than "any soft failure at
# all counts".
SOFT_FAIL_PARTIAL_RATIO = 0.2

# --- JS-shell detection (T6) -------------------------------------------------
#
# Same traefik incident, a level deeper: `extraction_too_short` already
# reported all 117 soft failures, but flattened two very different failure
# modes into one bucket. 115 of those 117 extracted to EXACTLY 61 characters
# — an identical byte length recurring across unrelated URLs, which is not
# a property genuinely-short-but-real pages have (their lengths scatter:
# 140, 142, 152, 162, 163, 167, 179, 180, 183, 185, 196, 197 in the same
# run). An identical length across many pages is the fingerprint of a
# client-side-rendered shell: the server returns the same static
# HTML/noscript template for every route, so trafilatura/BS4 extract
# byte-for-byte the same stub text no matter which URL was requested.
#
# The signal is inherently CROSS-PAGE — a single 61-char page is
# indistinguishable from a genuinely tiny page (see the "45 x 2" pair below,
# which must NOT be flagged). It only becomes conclusive once enough *other*
# pages in the SAME source land on the exact same length, so detection has
# to accumulate every soft-failed page's length across the whole crawl and
# only classify at the end of `sync_source`'s per-source loop — it cannot
# live in `extract.py`, which only ever sees one page at a time.
#
# MIN_SHELL_SIBLING_COUNT = 3: the real run's own false-candidate — 45 chars
# recurring on exactly 2 pages — is *not* a shell (two unrelated pages can
# coincidentally land on the same short length), so the threshold must
# clear 2. Requiring 3 does that with a full page of margin while still
# catching the real incident (61 chars x 115) by nearly two orders of
# magnitude, and it comfortably ignores the singleton lengths (140 x 1, 152
# x 1, ...) in the long tail of genuinely-short pages.
#
# Length-match tolerance = 0 (exact match, not a +/- N window): a shell page
# is the literal same static template re-served for every route, so
# extraction produces a byte-identical length every time — there is no
# reason for two real shell pages to differ even by one character. Grouping
# by exact length (rather than a fuzzy window) also avoids accidentally
# lumping together unrelated genuinely-short pages that merely happen to
# sit close to each other (e.g. 162 and 163, or 196 and 197 above) into a
# false shell group.
MIN_SHELL_SIBLING_COUNT = 3

# Circuit breaker for the headless-render retry loop (review finding #9):
# bail out of retrying further suspected shell pages once this many renders
# in a row have come back `None` (renderer down/unreachable/timing out),
# rather than paying the full RENDERER_CLIENT_TIMEOUT_S for every remaining
# suspect. 3 mirrors the tenacity retry-attempt count used elsewhere in this
# codebase (`_fetch_page_with_retry`) and is enough to distinguish "renderer
# had one bad page" from "renderer is not answering at all".
JS_RENDER_CIRCUIT_BREAKER_THRESHOLD = 3


def _detect_js_shell_pages(
    short_extractions: list[tuple[str, int]],
    *,
    min_sibling_count: int = MIN_SHELL_SIBLING_COUNT,
) -> dict[int, list[str]]:
    """Group `short_extractions` — `(url, length)` pairs for every page that
    soft-failed this sync's extraction as too-short — by their EXACT
    extracted length, and return only the length groups that meet
    `min_sibling_count` (see the module-level comment above for why exact
    length-matching and this threshold were chosen). A length appearing on
    fewer than `min_sibling_count` pages is left out entirely — it's treated
    as an ordinary genuinely-short page, not shell evidence.
    """
    by_length: dict[int, list[str]] = {}
    for url, length in short_extractions:
        by_length.setdefault(length, []).append(url)
    return {length: urls for length, urls in by_length.items() if len(urls) >= min_sibling_count}


def get_dsn() -> str:
    """Build a libpq keyword/value DSN from the standard Postgres env vars."""
    return (
        f"host={os.environ.get('POSTGRES_HOST', 'db')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"dbname={os.environ.get('POSTGRES_DB', '')} "
        f"user={os.environ.get('POSTGRES_USER', '')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', '')}"
    )


def get_connection() -> psycopg.Connection:
    """Open a new (autocommit=True) connection using the standard env vars.

    TCP keepalives are enabled as defense in depth: a long sync (crawl +
    per-page writes interleaved) can otherwise sit on a connection that an
    intermediate network hop (e.g. a Docker bridge) considers idle and drops
    silently. keepalives_idle=30s / keepalives_count=5 means a dead peer is
    detected well within any single page's fetch+write window.
    """
    return psycopg.connect(
        get_dsn(),
        autocommit=True,
        keepalives=1,
        keepalives_idle=30,
        keepalives_count=5,
    )


def hash_markdown(markdown: str) -> str:
    """SHA-256 hex digest of extracted markdown — the page-level drift key."""
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def _embedding_literal(vec: list[float]) -> str:
    """pgvector text input format: `[0.1,0.2,...]`."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


@dataclass
class SourceOutcome:
    """Summary of one sync attempt for one source."""

    name: str
    pages_fetched: int = 0  # new/changed pages successfully (re)indexed
    pages_skipped: int = 0  # unchanged pages skipped
    pages_not_modified: int = 0  # pages/llms-index short-circuited by a 304 conditional GET
    pages_failed: int = 0  # pages that errored during extract/chunk/embed/store
    pages_soft_failed: int = 0  # pages skipped due to expected site quirks (e.g. 404/503 fetch, stub/placeholder content)
    pages_removed: int = 0  # pages deleted because absent from this crawl
    chunks_indexed: int = 0
    shell_suspected_count: int = 0  # of pages_soft_failed, how many are suspected JS-shell stubs (T6)
    pages_js_rendered: int = 0  # of shell_suspected_count, how many were recovered via the renderer (T7)
    status: str = "ok"  # ok | partial | failed
    error: str | None = None


def classify_sync(
    outcome: SourceOutcome,
    *,
    crawl_aborted_early: bool = False,
    purge_guard_refused: bool = False,
    crawl_truncated: bool = False,
) -> str:
    """Classify one sync attempt's final `outcome` into `ok` / `partial` /
    `failed`, as an explicit, directly-testable function of the outcome's
    counters plus the out-of-band signals the counters alone can't convey
    (`crawl_aborted_early`, `purge_guard_refused`, `crawl_truncated`).

    Rules, evaluated IN ORDER (first match wins):

      failed  - cancelled by user (`outcome.error == "Aborted by user"`)
      failed  - nothing indexed or confirmed this run:
                `pages_fetched + pages_skipped + pages_not_modified == 0`
                (a source where every page soft/hard-failed, or the crawl
                saw nothing at all, must never read as "ok" — that's
                exactly what let the 2026-07-26 incident hide 6 zero-page
                sources as healthy)
      partial - `purge_guard_refused` (the purge-ratio guard declining to
                delete anything is itself an incident worth surfacing, even
                though the pages it protected are still intact)
      partial - `crawl_aborted_early` (the crawl broke off mid-stream; every
                page it did reach may have succeeded, but the corpus is
                incomplete)
      partial - `crawl_truncated` (sitemap discovery hit `max_pages` before
                fully enumerating the in-scope corpus — see
                `crawler.discover_sitemap_urls`/`crawler.crawl`'s
                `crawl_summary` sentinel. Kept as ITS OWN parameter rather
                than folded into `crawl_aborted_early`: an abort is a
                mid-stream break with pages after the break point never
                visited at all, whereas a truncation is a crawl that ran to
                a clean, deliberate stop with every yielded page fully
                processed — same downstream consequence (an incomplete
                enumeration that must not license `_delete_missing_pages`),
                but a different cause an operator debugging a "partial"
                status needs to be able to tell apart, and a future caller
                may legitimately want to abort without having discovery
                truncated, or vice versa)
      partial - `pages_failed > 0` (at least one HARD pipeline failure)
      partial - soft-failure ratio exceeds `SOFT_FAIL_PARTIAL_RATIO`:
                `pages_soft_failed / pages_seen > SOFT_FAIL_PARTIAL_RATIO`,
                where `pages_seen` includes every page category (fetched,
                skipped, hard-failed, soft-failed, not-modified) — this is
                the traefik case: 0 hard failures, but 117/280 (42%) of
                pages silently lost to soft failures
      ok      - otherwise
    """
    if outcome.error == "Aborted by user":
        return "failed"

    if outcome.pages_fetched + outcome.pages_skipped + outcome.pages_not_modified == 0:
        return "failed"

    if purge_guard_refused:
        return "partial"

    if crawl_aborted_early:
        return "partial"

    if crawl_truncated:
        return "partial"

    if outcome.pages_failed > 0:
        return "partial"

    pages_seen = (
        outcome.pages_fetched
        + outcome.pages_skipped
        + outcome.pages_failed
        + outcome.pages_soft_failed
        + outcome.pages_not_modified
    )
    if pages_seen > 0 and outcome.pages_soft_failed / pages_seen > SOFT_FAIL_PARTIAL_RATIO:
        return "partial"

    return "ok"


def ensure_source(conn: psycopg.Connection, source: SourceConfig) -> int:
    """Ensure a `doc_sources` row exists for `source`, returning its id.

    `base_url` is only ever overwritten from `EXCLUDED.base_url` (the value
    passed in `source`) when the EXISTING row is itself a crawl-type source.
    An existing upload-type row's `base_url` (the fixed `upload://{name}`
    sentinel — see `SourceConfig._base_url_matches_source_type`) is always
    preserved as-is, never clobbered by whatever `source.base_url` happens to
    be on this call — this function is called on every crawl sync (see
    `sync_source`, which itself refuses to run at all for an upload-type
    source, so in practice `source` here is always crawl-shaped; this guard
    is defense in depth against a stale/derived `SourceConfig` for the same
    `name` being passed in some other call path).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO doc_sources (name, base_url)
            VALUES (%s, %s)
            ON CONFLICT (name) DO UPDATE SET base_url = CASE
                WHEN doc_sources.source_type = 'upload' THEN doc_sources.base_url
                ELSE EXCLUDED.base_url
            END
            RETURNING id
            """,
            (source.name, str(source.base_url)),
        )
        row = cur.fetchone()
        assert row is not None
        source_id = row[0]
    if not conn.autocommit:
        conn.commit()
    return source_id


def get_existing_page_hash(conn: psycopg.Connection, url: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT content_hash FROM doc_pages WHERE url = %s", (url,))
        row = cur.fetchone()
        return row[0] if row else None


def replace_page(
    conn: psycopg.Connection,
    source_id: int,
    url: str,
    content_hash: str,
    chunks: list[dict],
    *,
    fts_config: str = "english",
    etag: str | None = None,
    last_modified: str | None = None,
) -> int:
    """Delete-and-reinsert a page + its chunks in a single transaction.

    `ON DELETE CASCADE` on `doc_chunks.page_id` means deleting the
    `doc_pages` row wipes its old chunks; the new page row (fresh id) and
    chunks are then inserted. Returns the number of chunks inserted.

    `fts_config` is the Postgres text-search configuration (`source.language`)
    stamped onto every inserted chunk's `fts_config` column, which drives the
    GENERATED `fts` column's `to_tsvector(fts_config, content)`. `etag` /
    `last_modified` are the page's HTTP validators (when the crawler surfaced
    them on a 200 response) persisted onto `doc_pages` for a future
    conditional-GET sync.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("DELETE FROM doc_pages WHERE url = %s", (url,))
            cur.execute(
                """
                INSERT INTO doc_pages (source_id, url, content_hash, etag, last_modified)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (source_id, url, content_hash, etag, last_modified),
            )
            row = cur.fetchone()
            assert row is not None
            page_id = row[0]
            for chunk in chunks:
                cur.execute(
                    """
                    INSERT INTO doc_chunks (page_id, heading_path, chunk_index, content, embedding, fts_config)
                    VALUES (%s, %s, %s, %s, %s::vector, %s::regconfig)
                    """,
                    (
                        page_id,
                        chunk["heading_path"],
                        chunk["chunk_index"],
                        chunk["content"],
                        _embedding_literal(chunk["embedding"]),
                        fts_config,
                    ),
                )
    return len(chunks)


def load_page_validators(
    conn: psycopg.Connection, source_id: int
) -> dict[str, tuple[str | None, str | None]]:
    """Return `{url: (etag, last_modified)}` for every `doc_pages` row of
    `source_id`, for `crawler.crawl`'s `conditional` argument."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT url, etag, last_modified FROM doc_pages WHERE source_id = %s",
            (source_id,),
        )
        rows = cur.fetchall()
    return {url: (etag, last_modified) for url, etag, last_modified in rows}


def update_page_validators(
    conn: psycopg.Connection, url: str, etag: str | None, last_modified: str | None
) -> None:
    """Refresh a `doc_pages` row's HTTP validators in place (used on the
    hash-unchanged path so a future sync can issue a conditional GET even
    when this run wasn't itself conditional)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE doc_pages SET etag = %s, last_modified = %s WHERE url = %s",
            (etag, last_modified, url),
        )
    if not conn.autocommit:
        conn.commit()


def get_llms_validators(conn: psycopg.Connection, source_id: int) -> tuple[str | None, str | None]:
    """Return `(llms_etag, llms_last_modified)` for `source_id`'s `doc_sources` row."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT llms_etag, llms_last_modified FROM doc_sources WHERE id = %s",
            (source_id,),
        )
        row = cur.fetchone()
    if row is None:
        return (None, None)
    return (row[0], row[1])


def set_llms_validators(
    conn: psycopg.Connection, source_id: int, etag: str | None, last_modified: str | None
) -> None:
    """Persist the llms-index (`llms.txt`/`llms-full.txt`) HTTP validators
    for a future conditional-GET sync."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE doc_sources SET llms_etag = %s, llms_last_modified = %s WHERE id = %s",
            (etag, last_modified, source_id),
        )
    if not conn.autocommit:
        conn.commit()


def _delete_missing_pages(
    conn: psycopg.Connection,
    source_id: int,
    seen_urls: set[str],
    *,
    force_delete_all: bool = False,
    existing_count: int | None = None,
    successful_seen_count: int | None = None,
    guard_refused_out: list[bool] | None = None,
) -> int:
    """Delete `doc_pages` rows for `source_id` whose URL was not seen in this
    crawl (i.e. removed upstream, or unreachable this run).

    `guard_refused_out`, if given, is a caller-owned list this function
    appends `True` to when (and only when) the purge-RATIO guard (case 2
    below) is the reason nothing was deleted — as opposed to the
    unconditional empty-`seen_urls` refusal (case 1) or simply having
    nothing to delete. Callers (namely `sync_source`) use this to feed
    `classify_sync`'s `purge_guard_refused` signal without duplicating the
    ratio math here.

    Two layers of defense in depth, both bypassed by `force_delete_all=True`
    (an explicit, deliberate "wipe this source" opt-in — `sync_source` never
    passes it; this function is not where that operation should live):

    1. An EMPTY `seen_urls` is always refused as a no-op. An empty seen-set
       almost never means "this source legitimately has zero pages now" —
       far more likely a transient upstream failure (DNS blip, 5xx,
       timed-out sitemap) made the crawl see nothing, and deleting
       everything on that basis is the single most destructive thing this
       pipeline can do.

    2. For sources with at least `PURGE_RATIO_GUARD_MIN_EXISTING_PAGES`
       existing pages, a large deletion is refused UNLESS it's paired with
       comparably large crawl coverage — see the module-level comment above
       `PURGE_DELETE_RATIO_THRESHOLD` for the full rationale. Concretely,
       measured against the traefik repair this guard exists for (397
       existing `doc_pages` rows):

         repair (must be PERMITTED):     fetched ~280 -> coverage 0.705
                                          purge   ~232 -> delete ratio 0.584
         BFS collapse (must be REFUSED): fetched ~60  -> coverage 0.151
                                          purge   ~337 -> delete ratio 0.849

       The repair's delete ratio (0.584) is actually the higher of the two,
       which is exactly why ratio alone can't distinguish them — its
       coverage (0.705) is what clears it: comfortably above the 0.3 floor
       (margin +0.405), while the collapse's coverage (0.151) comfortably
       misses it (margin -0.149). The guard only refuses when BOTH the
       delete ratio exceeds `PURGE_DELETE_RATIO_THRESHOLD` AND the coverage
       ratio is below `PURGE_FETCH_COVERAGE_FLOOR` — a large purge with
       healthy coverage (the repair) is allowed through.

    Note on Snapshot Semantics (S1 & S3 compatibility):
    Under S1 autocommit (`replace_page` commits each page immediately as iterated),
    querying `SELECT count(*) FROM doc_pages` inside `_delete_missing_pages` observes
    a post-crawl snapshot where newly discovered pages have already been inserted,
    distorting `delete_ratio` downward and `coverage_ratio` upward relative to the
    calibrated pre-sync thresholds. Furthermore, under S3 (`fetch_ok=False` items added
    to `seen_urls` to protect existing rows from being deleted), including fetch failures
    in `coverage_ratio` would allow a mass-fetch-failure run to clear the 0.3 floor.
    Therefore, `_delete_missing_pages` evaluates against `existing_count` (the pre-sync
    row count of `doc_pages` before any new insertions) and `successful_seen_count`
    (the count of `seen_urls` excluding `fetch_ok=False` failures).
    """
    if existing_count is None:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM doc_pages WHERE source_id = %s", (source_id,))
            (existing_count,) = cur.fetchone()

    if successful_seen_count is None:
        successful_seen_count = len(seen_urls)

    if existing_count == 0:
        return 0  # nothing exists to delete

    if not force_delete_all:
        if not seen_urls:
            logger.warning(
                "delete_missing_pages_empty_seen_urls_refused",
                source_id=source_id,
                existing_count=existing_count,
                hint="pass force_delete_all=True for an intentional full wipe",
            )
            return 0

        if existing_count >= PURGE_RATIO_GUARD_MIN_EXISTING_PAGES:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM doc_pages WHERE source_id = %s AND url <> ALL(%s)",
                    (source_id, list(seen_urls)),
                )
                (would_delete_count,) = cur.fetchone()
            delete_ratio = would_delete_count / existing_count
            coverage_ratio = successful_seen_count / existing_count
            if (
                delete_ratio > PURGE_DELETE_RATIO_THRESHOLD
                and coverage_ratio < PURGE_FETCH_COVERAGE_FLOOR
            ):
                logger.warning(
                    "delete_missing_pages_purge_ratio_guard_refused",
                    source_id=source_id,
                    existing_count=existing_count,
                    would_delete_count=would_delete_count,
                    delete_ratio=round(delete_ratio, 3),
                    seen_count=len(seen_urls),
                    successful_seen_count=successful_seen_count,
                    coverage_ratio=round(coverage_ratio, 3),
                    delete_ratio_threshold=PURGE_DELETE_RATIO_THRESHOLD,
                    coverage_ratio_floor=PURGE_FETCH_COVERAGE_FLOOR,
                    hint="pass force_delete_all=True for an intentional large purge",
                )
                if guard_refused_out is not None:
                    guard_refused_out.append(True)
                return 0

    with conn.cursor() as cur:
        if seen_urls:
            cur.execute(
                "DELETE FROM doc_pages WHERE source_id = %s AND url <> ALL(%s)",
                (source_id, list(seen_urls)),
            )
        else:
            cur.execute("DELETE FROM doc_pages WHERE source_id = %s", (source_id,))
        removed = cur.rowcount
    if not conn.autocommit:
        conn.commit()
    return removed


def _update_source_status(conn: psycopg.Connection, name: str, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE doc_sources SET last_synced = now(), last_status = %s WHERE name = %s",
            (status, name),
        )
    if not conn.autocommit:
        conn.commit()


def mark_source_failed(name: str) -> None:
    """Best-effort: persist `last_status='failed'` for `name` on a FRESH
    connection.

    Used when `sync_source` itself crashed (e.g. the connection it was using
    died mid-crawl) and therefore could not reach its own
    `_update_source_status` call — the failure mode that used to leave
    `doc_sources.last_status` as NULL, indistinguishable from "never ran".
    The crash may be a dead connection, so this always opens a new one.
    Never raises: any failure here (including a still-unreachable DB) is
    logged and swallowed so it can't mask the original crash.
    """
    try:
        conn = get_connection()
        try:
            _update_source_status(conn, name, "failed")
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 - best-effort, must not mask original error
        logger.error("mark_source_failed_also_failed", source=name, error=str(e))


def sync_source(
    source: SourceConfig,
    conn: psycopg.Connection,
    progress_cb: Callable[[SourceOutcome, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> SourceOutcome:
    """Run one full sync of `source` against `conn`. Never raises for
    per-page failures (recorded in the outcome); only a source-level failure
    (e.g. the crawl itself raising) short-circuits with status="failed".

    On receiving `fetch_ok=False` from `crawler.crawl()`: adds the URL to
    `seen_urls` (protecting the existing row from purge by `_delete_missing_pages`),
    increments `pages_soft_failed`, logs a distinct `page_fetch_skipped` event, and
    continues without touching the existing row in the database.

    Raises `ValueError` immediately (before touching the database or the
    network) if `source.source_type == 'upload'` — an upload-type source has
    no URL to crawl (`base_url` is the `upload://{name}` sentinel, not a real
    address); its content is indexed via `ingest_uploaded_docs` instead. This
    keeps a crawl-only entrypoint from ever trying to fetch an
    `upload://...` "URL" as if it were real.
    """
    if source.source_type == "upload":
        raise ValueError(
            f"cannot crawl-sync a source with source_type='upload' (source={source.name!r}); "
            "use ingest_uploaded_docs for upload sources instead"
        )

    outcome = SourceOutcome(name=source.name)
    log = logger.bind(source=source.name)

    source_id = ensure_source(conn, source)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM doc_pages WHERE source_id = %s", (source_id,))
        (pre_sync_existing_count,) = cur.fetchone()

    # Build the conditional-GET validator map passed into `crawler.crawl`:
    # per-page validators from `doc_pages`, plus (when llms.txt discovery is
    # enabled) the two llms-index candidate URLs mapped to the source's
    # stored llms validators, so an unchanged llms.txt/llms-full.txt can
    # short-circuit the whole crawl via a single 304.
    conditional: dict[str, tuple[str | None, str | None]] = load_page_validators(conn, source_id)
    if source.llms_txt in ("auto", "only"):
        parsed_base = urlparse(str(source.base_url))
        origin = f"{parsed_base.scheme}://{parsed_base.netloc}"
        llms_validators = get_llms_validators(conn, source_id)
        if llms_validators != (None, None):
            conditional[f"{origin}/llms-full.txt"] = llms_validators
            conditional[f"{origin}/llms.txt"] = llms_validators

    try:
        try:
            page_iter = iter(crawler.crawl(source, conditional=conditional))
        except TypeError:
            # Defensive fallback for callers/tests that monkeypatch
            # `crawler.crawl` with a signature that doesn't accept
            # `conditional` (mirrors the same tolerance pattern used
            # elsewhere for `progress_cb`-less test doubles).
            page_iter = iter(crawler.crawl(source))
    except Exception as e:  # noqa: BLE001 - defensive: crawler.crawl is a generator
        # function in production, so *calling* it can't itself raise — this
        # branch only fires if `crawl` is swapped for a non-generator
        # callable that raises immediately (tests do this to simulate a
        # crawl-level failure; a future refactor could too). The realistic
        # crawl-level failure path in production is the `next()` loop below,
        # which logs the same "crawl_failed" event with phase="iteration".
        log.error("crawl_failed", phase="call", error=str(e))
        outcome.status = "failed"
        outcome.error = str(e)
        _update_source_status(conn, source.name, outcome.status)
        return outcome

    seen_urls: set[str] = set()
    fetch_failed_urls: set[str] = set()
    crawl_aborted_early = False
    llms_unchanged = False
    crawl_truncated = False
    unprocessed_child_sitemaps = 0
    # (url, extracted length) for every page soft-failed this run as
    # too-short — accumulated across the whole crawl so it can be examined
    # for cross-page length repetition (JS-shell detection, T6) once the
    # loop below finishes. See the `MIN_SHELL_SIBLING_COUNT` comment.
    short_extraction_lengths: list[tuple[str, int]] = []

    try:
        # `crawl()` is a generator: pages are pulled and committed one at a
        # time (interleaving fetch and DB write) so a crash mid-crawl — e.g.
        # the DB connection dying after minutes idle-free — still leaves
        # every already-committed page in place instead of losing the whole
        # source. This is also where any real crawl-level failure surfaces
        # (a generator function can't raise on the initial call above; any
        # exception it raises happens here, at whichever `next()` triggers
        # it — including the very first one).
        while True:
            if cancel_event and cancel_event.is_set():
                log.info("sync_aborted_by_user")
                outcome.status = "failed"
                outcome.error = "Aborted by user"
                crawl_aborted_early = True
                break

            try:
                page = next(page_iter)
            except StopIteration:
                break
            except Exception as e:  # noqa: BLE001 - crawl-level failure mid-iteration
                log.error("crawl_failed", phase="iteration", error=str(e))
                outcome.error = str(e)
                crawl_aborted_early = True
                break

            if page.get("kind") == "llms_index_unchanged":
                # The whole llms.txt/llms-full.txt index 304'd: nothing in
                # the source changed, so the existing `doc_pages` rows are
                # left completely untouched (no purge, no rewrite).
                llms_unchanged = True
                log.info("llms_index_unchanged", url=page.get("url"))
                break

            if page.get("kind") == "crawl_summary":
                # First-class (non-log) truncation signal from
                # `crawler.crawl` — see its docstring. Yielded as the last
                # item, so record it and keep iterating (the next `next()`
                # call raises StopIteration, ending the loop normally).
                crawl_truncated = bool(page.get("truncated_at_cap"))
                unprocessed_child_sitemaps = page.get("unprocessed_child_sitemaps", 0)
                log.info(
                    "crawl_truncated_received",
                    truncated_at_cap=crawl_truncated,
                    unprocessed_child_sitemaps=unprocessed_child_sitemaps,
                )
                continue

            url = page["url"]
            seen_urls.add(url)

            if not page.get("fetch_ok", True):
                fetch_failed_urls.add(url)
                outcome.pages_soft_failed += 1
                log.warning("page_fetch_skipped", url=url)
                if progress_cb:
                    progress_cb(outcome, url)
                continue

            if page.get("not_modified"):
                # Per-page 304: validators matched, body wasn't re-fetched.
                # The existing row is already correct — don't touch it.
                outcome.pages_not_modified += 1
                log.info("page_not_modified", url=url)
                if progress_cb:
                    progress_cb(outcome, url)
                continue

            try:
                markdown = page.get("markdown")
                if markdown is None:
                    # HTML page: unchanged extract.extract flow.
                    html = page["html"]
                    extraction = extract.extract(url, html)
                    if extraction.status != "ok":
                        outcome.pages_soft_failed += 1
                        short_extraction_lengths.append((url, extraction.length))
                        log.warning("page_content_skipped", url=url, reason=extraction.reason)
                        if progress_cb:
                            progress_cb(outcome, url)
                        continue
                    markdown = extraction.markdown
                # else: llms.txt section, already-extracted markdown — skip
                # extract.extract entirely.

                content_hash = hash_markdown(markdown)
                existing_hash = get_existing_page_hash(conn, url)
                if existing_hash == content_hash:
                    outcome.pages_skipped += 1
                    log.debug("page_unchanged_skip", url=url)
                    if page.get("etag") or page.get("last_modified"):
                        # Refresh validators even though the content itself
                        # didn't change, so a future sync can issue a
                        # conditional GET for this URL.
                        update_page_validators(conn, url, page.get("etag"), page.get("last_modified"))
                    if progress_cb:
                        progress_cb(outcome, url)
                    continue

                chunks = chunker.chunk_markdown(url, markdown)
                chunks = embedder.embed_chunks(chunks)
                n = replace_page(
                    conn,
                    source_id,
                    url,
                    content_hash,
                    chunks,
                    fts_config=source.language,
                    etag=page.get("etag"),
                    last_modified=page.get("last_modified"),
                )
                outcome.pages_fetched += 1
                outcome.chunks_indexed += n
                log.info("page_indexed", url=url, chunks=n, changed=existing_hash is not None)
                if progress_cb:
                    progress_cb(outcome, url)
            except Exception as e:  # noqa: BLE001 - isolate per-page failures
                if not conn.autocommit:
                    conn.rollback()
                outcome.pages_failed += 1
                log.error("page_index_failed", url=url, error=str(e))
                if progress_cb:
                    progress_cb(outcome, url)
    finally:
        # Explicitly close the generator on every exit path (StopIteration,
        # mid-iteration failure, or an unexpected exception) rather than
        # relying on GC to eventually run `crawl()`'s `finally` (which is
        # what actually closes its httpx.Client). `seen_urls`-driven code
        # below never touches `page_iter` again either way.
        close = getattr(page_iter, "close", None)
        if close is not None:
            close()

    # JS-shell detection (T6): only possible now, with every page's
    # extraction outcome for this source in hand — see
    # `_detect_js_shell_pages`'s docstring and the `MIN_SHELL_SIBLING_COUNT`
    # comment above for why this can't run per-page. Runs unconditionally
    # (including the `llms_unchanged` early-return path below) since
    # `short_extraction_lengths` is simply empty when no pages were
    # extracted this run.
    shell_suspected_urls: list[str] = []
    for length, urls in _detect_js_shell_pages(short_extraction_lengths).items():
        for suspect_url in urls:
            log.bind(url=suspect_url).warning(
                "js_shell_suspected",
                length=length,
                sibling_count=len(urls),
            )
        outcome.shell_suspected_count += len(urls)
        shell_suspected_urls.extend(urls)

    # Headless-render retry (T7), gated on the source's `js_render` opt-in:
    # only pages T6 just flagged above — never the whole
    # `short_extraction_lengths` set, and never at first-extraction time,
    # since the detector above is what makes that distinction possible in
    # the first place (a single too-short page is not evidence on its own;
    # see `_detect_js_shell_pages`). A source that hasn't set `js_render` is
    # completely unaffected: no renderer call is ever made for it.
    #
    # The renderer re-navigates to each URL itself (a real browser fetch),
    # so the original static HTML this sync already fetched is irrelevant
    # here — there is nothing to reuse from `short_extraction_lengths`
    # beyond the URL.
    #
    # Every failure mode (renderer disabled/unreachable/slow/erroring, or
    # the re-render still extracting too short) is a no-op: the page stays
    # exactly as soft-failed as it already was above. `renderer.render_page`
    # itself never raises and is time-bounded, so this loop can't hang a
    # sync even if the renderer is stuck.
    if source.js_render and shell_suspected_urls:
        render_limiter = crawler.RateLimiter(source.rate_limit_rps)
        consecutive_render_failures = 0
        for render_idx, suspect_url in enumerate(shell_suspected_urls):
            # Honor the source's configured crawl politeness for the retry
            # pass too — the renderer must not become a way to bypass it.
            render_limiter.wait()
            rendered_html = renderer.render_page(suspect_url)
            if rendered_html is None:
                log.info("js_render_retry_failed", url=suspect_url)
                consecutive_render_failures += 1
                if consecutive_render_failures >= JS_RENDER_CIRCUIT_BREAKER_THRESHOLD:
                    # Circuit breaker (review finding #9): with the renderer
                    # down (or unreachable), each suspected shell page costs
                    # a full RENDERER_CLIENT_TIMEOUT_S plus the rate-limiter
                    # wait for nothing. At real-world scale (upwards of a
                    # hundred suspected shell pages) that is tens of minutes
                    # of pure dead time appended to a sync. Soft-fail is
                    # still correct for the remaining URLs; just stop paying
                    # the per-URL renderer cost once it's clearly not
                    # working.
                    log.warning(
                        "js_render_circuit_breaker_tripped",
                        consecutive_failures=consecutive_render_failures,
                        remaining_urls=len(shell_suspected_urls) - render_idx - 1,
                    )
                    break
                continue
            consecutive_render_failures = 0

            re_extraction = extract.extract(suspect_url, rendered_html)
            if re_extraction.status != "ok":
                log.info(
                    "js_render_retry_still_short",
                    url=suspect_url,
                    length=re_extraction.length,
                )
                continue

            try:
                markdown = re_extraction.markdown
                assert markdown is not None
                content_hash = hash_markdown(markdown)
                existing_hash = get_existing_page_hash(conn, suspect_url)
                if existing_hash == content_hash:
                    outcome.pages_soft_failed -= 1
                    outcome.pages_skipped += 1
                    outcome.pages_js_rendered += 1
                    log.info("js_render_retry_unchanged", url=suspect_url)
                    continue

                chunks = chunker.chunk_markdown(suspect_url, markdown)
                chunks = embedder.embed_chunks(chunks)
                n = replace_page(
                    conn,
                    source_id,
                    suspect_url,
                    content_hash,
                    chunks,
                    fts_config=source.language,
                    # No etag/last_modified: this content came from a
                    # headless render, not the HTTP response that was
                    # actually soft-failed, so there is no real validator
                    # to persist. The next normal sync just re-fetches in
                    # full, same as any page with no stored validators.
                )
                outcome.pages_soft_failed -= 1
                outcome.pages_fetched += 1
                outcome.pages_js_rendered += 1
                outcome.chunks_indexed += n
                log.info("js_render_retry_indexed", url=suspect_url, chunks=n)
            except Exception as e:  # noqa: BLE001 - isolate per-page failures, mirrors the main loop
                if not conn.autocommit:
                    conn.rollback()
                log.error("js_render_retry_index_failed", url=suspect_url, error=str(e))

    if llms_unchanged:
        # The llms-index 304'd as a whole: nothing about the source changed,
        # so every existing `doc_pages` row for it is still correct.
        # Deliberately skip `_delete_missing_pages` entirely (zero deletes,
        # not just a guard-refused delete) rather than routing through the
        # `not seen_urls` empty-purge-refusal path below, since `seen_urls`
        # is empty here for a reason unrelated to a broken enumeration.
        outcome.pages_removed = 0
        outcome.pages_not_modified = pre_sync_existing_count
        outcome.status = "ok"
        _update_source_status(conn, source.name, outcome.status)
        log.info(
            "source_sync_complete",
            status=outcome.status,
            reason="llms_index_unchanged",
            pages_not_modified=outcome.pages_not_modified,
        )
        return outcome

    # `seen_urls` is only a trustworthy, COMPLETE enumeration of the source
    # when the crawl actually reached `StopIteration` having enumerated the
    # full in-scope corpus. Three distinct unsafe cases all funnel through
    # here and must all be refused:
    #   1. `crawl_aborted_early` — the crawl broke off mid-stream; pages
    #      after the break point were never (re)visited, so purging
    #      "missing" pages would delete everything the crawl hadn't reached
    #      yet (in the worst case, an abort on page 1, that's every page).
    #   2. A crawl that ran to completion but yielded ZERO pages — e.g. a
    #      transient upstream outage (sitemap 5xx, DNS blip) that BFS/sitemap
    #      fallback swallows into "nothing found" rather than an exception.
    #      `crawl_aborted_early` is False here, but `seen_urls` is just as
    #      untrustworthy: it says nothing about which pages still exist
    #      upstream, only that the crawler saw none of them this run.
    #   3. `crawl_truncated` — sitemap discovery hit `max_pages` (or ran out
    #      of budget before processing every child sitemap) before it could
    #      enumerate the full in-scope corpus. Unlike case 1, every page the
    #      crawl DID yield was fully processed — but `seen_urls` is still
    #      only an arbitrary SLICE of what exists upstream, not the whole
    #      corpus, so a page's absence proves nothing about whether it was
    #      removed. This is the 2026 sync-drift incident: two sources
    #      (`gemini-api`, `google-search-console-api`) fetched exactly as
    #      many pages as they deleted, every run, because a different
    #      arbitrary slice was sampled and the previous run's slice read as
    #      "missing".
    # `_delete_missing_pages` also refuses an empty `seen_urls` on its own
    # (defense in depth) — this check exists to log the *reason* for
    # skipping in a way that's specific to sync_source's cases.
    purge_guard_refused_flag: list[bool] = []
    if crawl_aborted_early or crawl_truncated or not seen_urls:
        outcome.pages_removed = 0
        if crawl_aborted_early:
            reason = "crawl_aborted_early"
        elif crawl_truncated:
            reason = "crawl_truncated"
        else:
            reason = "completed_with_zero_pages_seen"
        log.info(
            "stale_page_purge_skipped",
            reason=reason,
            pages_seen=len(seen_urls),
            unprocessed_child_sitemaps=unprocessed_child_sitemaps,
        )
    else:
        outcome.pages_removed = _delete_missing_pages(
            conn,
            source_id,
            seen_urls,
            existing_count=pre_sync_existing_count,
            successful_seen_count=len(seen_urls - fetch_failed_urls),
            guard_refused_out=purge_guard_refused_flag,
        )
    purge_guard_refused = bool(purge_guard_refused_flag)

    if cancel_event and cancel_event.is_set():
        outcome.status = "failed"
        outcome.error = "Aborted by user"
    else:
        outcome.status = classify_sync(
            outcome,
            crawl_aborted_early=crawl_aborted_early,
            purge_guard_refused=purge_guard_refused,
            crawl_truncated=crawl_truncated,
        )

    _update_source_status(conn, source.name, outcome.status)
    log.info(
        "source_sync_complete",
        status=outcome.status,
        pages_fetched=outcome.pages_fetched,
        pages_skipped=outcome.pages_skipped,
        pages_not_modified=outcome.pages_not_modified,
        pages_failed=outcome.pages_failed,
        pages_soft_failed=outcome.pages_soft_failed,
        pages_removed=outcome.pages_removed,
        chunks_indexed=outcome.chunks_indexed,
        shell_suspected_count=outcome.shell_suspected_count,
        pages_js_rendered=outcome.pages_js_rendered,
        crawl_truncated=crawl_truncated,
    )
    return outcome


def sync_source_with_metrics(
    source: SourceConfig,
    conn: psycopg.Connection,
    progress_cb: Callable[[SourceOutcome, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> SourceOutcome:
    """Instrumented wrapper around `sync_source` -- the ONE call every real
    sync entrypoint (admin.py's manual/full-sync routes, `main.py`'s
    `POST /sync` route, and the scheduler) should use instead of calling
    `sync_source` directly, so a Prometheus sample is recorded for every
    completed sync regardless of which layer triggered it (see
    `app.metrics` for the recording logic and why it lives in a separate
    leaf module rather than here or in `main.py`).

    Calls the module-level `sync_source` by its bare name (not
    `self.sync_source`) so a test that monkeypatches `store.sync_source`
    (e.g. `ingestion/tests/test_sync_health.py`'s admin-path characterization
    test) is still honored here -- this wrapper adds instrumentation, it
    does not hardcode which underlying implementation runs.

    Tolerates a `sync_source` double with a narrower signature (no
    `cancel_event`, or no `progress_cb`/`cancel_event` at all), e.g. a test
    monkeypatch. This is done by inspecting `sync_source`'s signature ONCE
    up front and only passing the keyword arguments it actually accepts,
    rather than calling it and catching `TypeError` -- a `TypeError` raised
    from deep inside a real `sync_source` body (extract/chunk/embed) is
    indistinguishable from a signature mismatch, so a catch-and-retry ladder
    here would silently re-run the ENTIRE sync (a second full crawl, then a
    third) before finally propagating the error (review finding #5).
    """
    start = time.monotonic()
    kwargs: dict[str, Any] = {}
    try:
        sig_params = inspect.signature(sync_source).parameters
    except (TypeError, ValueError):
        sig_params = {}
    accepts_var_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig_params.values())
    if accepts_var_kwargs or "progress_cb" in sig_params:
        kwargs["progress_cb"] = progress_cb
    if accepts_var_kwargs or "cancel_event" in sig_params:
        kwargs["cancel_event"] = cancel_event
    outcome = sync_source(source, conn, **kwargs)
    duration = time.monotonic() - start
    metrics.record_sync_outcome(source.name, outcome, duration)
    return outcome


def sync_all(
    sources: list[SourceConfig],
    progress_cb: Callable[[SourceOutcome, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, SourceOutcome]:
    """Sync each of `sources` in turn, each with its own connection.

    Any source with `source_type == 'upload'` is SKIPPED entirely — not
    attempted, not errored on, simply excluded from the batch (and therefore
    absent from the returned dict). Upload sources have no crawl to run
    (`sync_source` itself now refuses to run for one — see its docstring);
    their content is indexed via `ingest_uploaded_docs` instead, on a
    completely separate call path (the admin upload route). This mirrors
    `sources_repo.due_sources()`'s exclusion at the SQL layer, but is
    enforced explicitly here too since `sync_all`'s caller may build its
    `sources` list a different way than `due_sources` does.
    """
    results: dict[str, SourceOutcome] = {}
    for source in sources:
        if source.source_type == "upload":
            logger.info("sync_all_skipped_upload_source", source=source.name)
            continue
        if cancel_event and cancel_event.is_set():
            results[source.name] = SourceOutcome(name=source.name, status="failed", error="Aborted by user")
            continue
        conn = get_connection()
        try:
            results[source.name] = sync_source_with_metrics(
                source, conn, progress_cb=progress_cb, cancel_event=cancel_event
            )
        finally:
            conn.close()
    return results


def ingest_uploaded_docs(
    conn: psycopg.Connection,
    source: SourceRecord,
    docs: list[UploadedDoc],
    *,
    progress_cb: Callable[[SourceOutcome, str], None] | None = None,
) -> SourceOutcome:
    """Index a batch of already-parsed `UploadedDoc`s (from `app.uploads`)
    into `source`'s `doc_pages`/`doc_chunks`, using the same hash-diff
    skip/re-embed idiom `sync_source` uses per page, but over an in-memory
    document batch instead of a live crawl.

    Per doc: the page URL is the sentinel `upload://{source.name}/{doc.rel_path}`.
    `hash_markdown(doc.markdown)` is compared against the existing
    `doc_pages.content_hash` for that URL (via `get_existing_page_hash`) —
    unchanged content is skipped (`pages_skipped` +1, no re-chunk/re-embed,
    same as `sync_source`'s unchanged-page path); new or changed content is
    run through `chunker.chunk_markdown` -> `embedder.embed_chunks` ->
    `replace_page`, exactly the same call shapes `sync_source` uses.

    Raises `ValueError` immediately (before touching anything) if
    `source.source_type != 'upload'` — this function must never be pointed
    at a crawl source; use `sync_source`/`sync_source_with_metrics` for
    those.

    Deliberately NEVER calls `_delete_missing_pages`: unlike a crawl, one
    upload batch is never a complete enumeration of the source's pages (a
    user can upload a handful of files at a time, across many separate
    batches), so a page's absence from THIS batch is never evidence it was
    removed. Any page from a prior batch simply survives untouched.

    Status classification reuses `classify_sync` with its default
    (crawl-only) signals all left at their defaults (`crawl_aborted_early`/
    `purge_guard_refused`/`crawl_truncated` all False, which don't apply to
    an upload batch) — this already yields exactly the required semantics
    for an upload batch: `pages_fetched + pages_skipped == 0` (nothing
    succeeded) -> "failed"; `pages_failed > 0` with at least one success ->
    "partial"; otherwise -> "ok".

    `progress_cb`, when given, is called as `progress_cb(outcome, url)`
    after every doc (success, skip, or failure) — identical signature and
    per-item cadence to `sync_source`'s `progress_cb`, so a caller (the admin
    upload route) can wire live progress into the same `_sync_status` widget
    used for crawl syncs.
    """
    if source.source_type != "upload":
        raise ValueError(
            f"ingest_uploaded_docs cannot be called for source {source.name!r}: "
            f"source_type={source.source_type!r} (must be 'upload')"
        )

    start = time.monotonic()
    outcome = SourceOutcome(name=source.name)
    log = logger.bind(source=source.name)

    for doc in docs:
        url = f"upload://{source.name}/{doc.rel_path}"
        try:
            content_hash = hash_markdown(doc.markdown)
            existing_hash = get_existing_page_hash(conn, url)
            if existing_hash == content_hash:
                outcome.pages_skipped += 1
                log.debug("page_unchanged_skip", url=url)
                if progress_cb:
                    progress_cb(outcome, url)
                continue

            chunks = chunker.chunk_markdown(url, doc.markdown)
            chunks = embedder.embed_chunks(chunks)
            n = replace_page(
                conn,
                source.id,
                url,
                content_hash,
                chunks,
                fts_config=source.language,
            )
            outcome.pages_fetched += 1
            outcome.chunks_indexed += n
            log.info("page_indexed", url=url, chunks=n, changed=existing_hash is not None)
            if progress_cb:
                progress_cb(outcome, url)
        except Exception as e:  # noqa: BLE001 - isolate per-doc failures, mirrors sync_source
            if not conn.autocommit:
                conn.rollback()
            outcome.pages_failed += 1
            log.error("page_index_failed", url=url, error=str(e))
            if progress_cb:
                progress_cb(outcome, url)

    outcome.status = classify_sync(outcome)
    _update_source_status(conn, source.name, outcome.status)

    duration = time.monotonic() - start
    metrics.record_sync_outcome(source.name, outcome, duration)

    log.info(
        "source_sync_complete",
        status=outcome.status,
        pages_fetched=outcome.pages_fetched,
        pages_skipped=outcome.pages_skipped,
        pages_failed=outcome.pages_failed,
        chunks_indexed=outcome.chunks_indexed,
    )
    return outcome


@dataclass(frozen=True)
class PageRecord:
    id: int
    source_id: int
    source_name: str
    url: str
    content_hash: str
    fetched_at: datetime
    chunk_count: int


@dataclass(frozen=True)
class ChunkRecord:
    id: int
    heading_path: str | None
    chunk_index: int
    content: str


def _row_to_page_record(row: tuple) -> PageRecord:
    id_, source_id, source_name, url, content_hash, fetched_at, chunk_count = row
    return PageRecord(
        id=id_,
        source_id=source_id,
        source_name=source_name,
        url=url,
        content_hash=content_hash,
        fetched_at=fetched_at,
        chunk_count=chunk_count,
    )


def _row_to_chunk_record(row: tuple) -> ChunkRecord:
    id_, heading_path, chunk_index, content = row
    return ChunkRecord(
        id=id_,
        heading_path=heading_path,
        chunk_index=chunk_index,
        content=content,
    )


def list_doc_pages(
    conn: psycopg.Connection,
    *,
    source_id: int | None = None,
    query: str | None = None,
    limit: int = 100,
) -> list[PageRecord]:
    """Fetch `doc_pages` rows joined with `doc_sources` and count of `doc_chunks`,
    optionally filtered by `source_id` or `query` (URL/heading/content search)."""
    where_clauses = ["1=1"]
    params: list[Any] = []
    if source_id is not None:
        where_clauses.append("p.source_id = %s")
        params.append(source_id)
    if query:
        pattern = f"%{query}%"
        where_clauses.append(
            "(p.url ILIKE %s OR EXISTS ("
            "SELECT 1 FROM doc_chunks c2 WHERE c2.page_id = p.id AND "
            "(c2.heading_path ILIKE %s OR c2.content ILIKE %s)"
            "))"
        )
        params.extend([pattern, pattern, pattern])
    params.append(limit)
    where_sql = " AND ".join(where_clauses)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT p.id, p.source_id, s.name AS source_name, p.url, p.content_hash, p.fetched_at, COUNT(c.id) AS chunk_count
            FROM doc_pages p
            JOIN doc_sources s ON p.source_id = s.id
            LEFT JOIN doc_chunks c ON c.page_id = p.id
            WHERE {where_sql}
            GROUP BY p.id, p.source_id, s.name, p.url, p.content_hash, p.fetched_at
            ORDER BY p.fetched_at DESC
            LIMIT %s
            """,
            tuple(params),
        )
        rows = cur.fetchall()
    return [_row_to_page_record(row) for row in rows]


def get_page_chunks(conn: psycopg.Connection, page_id: int) -> list[ChunkRecord]:
    """Fetch all `doc_chunks` for a specific `page_id` ordered by `chunk_index`."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, heading_path, chunk_index, content
            FROM doc_chunks
            WHERE page_id = %s
            ORDER BY chunk_index ASC
            """,
            (page_id,),
        )
        rows = cur.fetchall()
    return [_row_to_chunk_record(row) for row in rows]


def get_chunk_by_id(conn: psycopg.Connection, chunk_id: int) -> dict[str, Any] | None:
    """Fetch a single `doc_chunk` by ID joined with its parent page and source."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, s.name AS source, c.heading_path, p.url, c.content, p.fetched_at
            FROM doc_chunks c
            JOIN doc_pages p ON c.page_id = p.id
            JOIN doc_sources s ON p.source_id = s.id
            WHERE c.id = %s
            """,
            (chunk_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "source": row[1],
        "heading_path": row[2],
        "url": row[3],
        "content": row[4],
        "fetched_at": row[5].isoformat() if row[5] else None,
    }


def get_source_tree(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Fetch hierarchical overview of indexed sources with page and chunk totals."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.name, s.base_url,
                   COUNT(DISTINCT p.id) AS page_count,
                   COUNT(c.id) AS chunk_count,
                   s.last_synced
            FROM doc_sources s
            LEFT JOIN doc_pages p ON p.source_id = s.id
            LEFT JOIN doc_chunks c ON c.page_id = p.id
            GROUP BY s.id, s.name, s.base_url, s.last_synced
            ORDER BY s.name ASC
            """
        )
        rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "name": r[1],
            "base_url": r[2],
            "page_count": r[3],
            "chunk_count": r[4],
            "last_synced": r[5].isoformat() if r[5] else None,
        }
        for r in rows
    ]


def search_chunks(
    conn: psycopg.Connection,
    query: str,
    *,
    source: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Perform hybrid RRF search (vector + fts) over doc_chunks, returning raw chunk dictionary items."""
    from .embedder import get_model

    query_prompt = os.environ.get(
        "EMBEDDING_QUERY_PROMPT",
        "Represent this sentence for searching relevant passages: ",
    )
    model = get_model()
    (vec,) = list(model.embed([query_prompt + query]))
    query_vec_str = "[" + ",".join(repr(float(x)) for x in vec) + "]"

    sql = """
    WITH vector_candidates AS (
        SELECT dc.id, dc.embedding <=> %(query_vec)s::vector AS distance
        FROM doc_chunks dc
        JOIN doc_pages dp ON dp.id = dc.page_id
        JOIN doc_sources ds ON ds.id = dp.source_id
        WHERE %(source)s::text IS NULL OR ds.name = %(source)s
        ORDER BY dc.embedding <=> %(query_vec)s::vector
        LIMIT 30
    ),
    vector_arm AS (
        SELECT id, row_number() OVER (ORDER BY distance) AS rnk
        FROM vector_candidates
    ),
    fts_candidates AS (
        SELECT dc.id,
               ts_rank(dc.fts, websearch_to_tsquery(dc.fts_config, %(query_text)s)) AS rank_score
        FROM doc_chunks dc
        JOIN doc_pages dp ON dp.id = dc.page_id
        JOIN doc_sources ds ON ds.id = dp.source_id
        WHERE dc.fts @@ websearch_to_tsquery(dc.fts_config, %(query_text)s)
          AND (%(source)s::text IS NULL OR ds.name = %(source)s)
        ORDER BY rank_score DESC
        LIMIT 30
    ),
    fts_arm AS (
        SELECT id, row_number() OVER (ORDER BY rank_score DESC) AS rnk
        FROM fts_candidates
    ),
    fused AS (
        SELECT id, SUM(1.0 / (60 + rnk)) AS rrf_score
        FROM (
            SELECT id, rnk FROM vector_arm
            UNION ALL
            SELECT id, rnk FROM fts_arm
        ) arms
        GROUP BY id
    )
    SELECT dc.id, ds.name AS source, dc.heading_path, dp.url, dc.content, fused.rrf_score
    FROM fused
    JOIN doc_chunks dc ON dc.id = fused.id
    JOIN doc_pages dp ON dp.id = dc.page_id
    JOIN doc_sources ds ON ds.id = dp.source_id
    ORDER BY fused.rrf_score DESC
    LIMIT %(limit)s;
    """

    with conn.cursor() as cur:
        cur.execute(
            sql,
            {
                "query_vec": query_vec_str,
                "query_text": query,
                "source": source,
                "limit": limit,
            },
        )
        rows = cur.fetchall()

    results = []
    for r in rows:
        content = r[4] or ""
        snippet = content.replace("\n", " ").strip()
        if len(snippet) > 200:
            snippet = snippet[:197] + "..."
        results.append(
            {
                "id": r[0],
                "source": r[1],
                "heading_path": r[2],
                "url": r[3],
                "score": float(r[5]),
                "snippet": snippet,
            }
        )
    return results


def purge_source(conn: psycopg.Connection, source_id: int) -> int:
    """Delete all doc_pages (cascading to doc_chunks) for source_id,
    reset last_synced/last_status/llms validators to NULL.
    Returns the number of pages deleted.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM doc_pages WHERE source_id = %s", (source_id,))
        (count,) = cur.fetchone()

        cur.execute("DELETE FROM doc_pages WHERE source_id = %s", (source_id,))

        cur.execute(
            """
            UPDATE doc_sources
            SET last_synced = NULL,
                last_status = NULL,
                llms_etag = NULL,
                llms_last_modified = NULL
            WHERE id = %s
            """,
            (source_id,),
        )
    return count




