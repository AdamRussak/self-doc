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
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlparse

import psycopg

from . import chunker, crawler, embedder, extract
from .config import SourceConfig
from .logging_config import get_logger

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
    status: str = "ok"  # ok | partial | failed
    error: str | None = None


def ensure_source(conn: psycopg.Connection, source: SourceConfig) -> int:
    """Ensure a `doc_sources` row exists for `source`, returning its id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO doc_sources (name, base_url)
            VALUES (%s, %s)
            ON CONFLICT (name) DO UPDATE SET base_url = EXCLUDED.base_url
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
) -> int:
    """Delete `doc_pages` rows for `source_id` whose URL was not seen in this
    crawl (i.e. removed upstream, or unreachable this run).

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
) -> SourceOutcome:
    """Run one full sync of `source` against `conn`. Never raises for
    per-page failures (recorded in the outcome); only a source-level failure
    (e.g. the crawl itself raising) short-circuits with status="failed".

    On receiving `fetch_ok=False` from `crawler.crawl()`: adds the URL to
    `seen_urls` (protecting the existing row from purge by `_delete_missing_pages`),
    increments `pages_soft_failed`, logs a distinct `page_fetch_skipped` event, and
    continues without touching the existing row in the database.
    """
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

            url = page["url"]
            seen_urls.add(url)

            if not page.get("fetch_ok", True):
                fetch_failed_urls.add(url)
                outcome.pages_soft_failed += 1
                log.info("page_fetch_skipped", url=url)
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
                        log.info("page_content_skipped", url=url, reason=extraction.reason)
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
                    log.info("page_unchanged_skip", url=url)
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
    # when the crawl actually reached `StopIteration`. Two distinct unsafe
    # cases both funnel through here and must both be refused:
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
    # Either way, `_delete_missing_pages` also refuses an empty `seen_urls`
    # on its own (defense in depth) — this check exists to log the *reason*
    # for skipping in a way that's specific to sync_source's two cases.
    if crawl_aborted_early or not seen_urls:
        outcome.pages_removed = 0
        reason = "crawl_aborted_early" if crawl_aborted_early else "completed_with_zero_pages_seen"
        log.info("stale_page_purge_skipped", reason=reason, pages_seen=len(seen_urls))
    else:
        outcome.pages_removed = _delete_missing_pages(
            conn,
            source_id,
            seen_urls,
            existing_count=pre_sync_existing_count,
            successful_seen_count=len(seen_urls - fetch_failed_urls),
        )

    pages_seen = outcome.pages_fetched + outcome.pages_skipped + outcome.pages_failed + outcome.pages_soft_failed
    succeeded_any = outcome.pages_fetched > 0 or outcome.pages_skipped > 0 or outcome.pages_soft_failed > 0
    if pages_seen == 0:
        # A crawl that fetched/skipped/failed nothing indexed nothing — never
        # report "ok" for an empty crawl (defeats partial/failed alerting).
        outcome.status = "failed"
    elif outcome.pages_failed == 0:
        outcome.status = "ok"
    elif succeeded_any:
        outcome.status = "partial"
    else:
        outcome.status = "failed"

    if crawl_aborted_early and outcome.status == "ok":
        # The crawl itself didn't finish even though every page it did yield
        # succeeded — never report a fully-clean "ok" for an incomplete crawl.
        outcome.status = "partial"

    _update_source_status(conn, source.name, outcome.status)
    log.info(
        "source_sync_complete",
        status=outcome.status,
        pages_fetched=outcome.pages_fetched,
        pages_skipped=outcome.pages_skipped,
        pages_failed=outcome.pages_failed,
        pages_soft_failed=outcome.pages_soft_failed,
        pages_removed=outcome.pages_removed,
        chunks_indexed=outcome.chunks_indexed,
    )
    return outcome


def sync_all(
    sources: list[SourceConfig],
    progress_cb: Callable[[SourceOutcome, str], None] | None = None,
) -> dict[str, SourceOutcome]:
    """Sync each of `sources` in turn, each with its own connection."""
    results: dict[str, SourceOutcome] = {}
    for source in sources:
        conn = get_connection()
        try:
            results[source.name] = sync_source(source, conn, progress_cb=progress_cb)
        finally:
            conn.close()
    return results


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

