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

import psycopg

from . import chunker, crawler, embedder, extract
from .config import SourceConfig
from .logging_config import get_logger

logger = get_logger(component="store")


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
    """Open a new (autocommit=False) connection using the standard env vars."""
    return psycopg.connect(get_dsn())


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
    pages_failed: int = 0  # pages that errored during extract/chunk/embed/store
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
) -> int:
    """Delete-and-reinsert a page + its chunks in a single transaction.

    `ON DELETE CASCADE` on `doc_chunks.page_id` means deleting the
    `doc_pages` row wipes its old chunks; the new page row (fresh id) and
    chunks are then inserted. Returns the number of chunks inserted.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("DELETE FROM doc_pages WHERE url = %s", (url,))
            cur.execute(
                """
                INSERT INTO doc_pages (source_id, url, content_hash)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (source_id, url, content_hash),
            )
            row = cur.fetchone()
            assert row is not None
            page_id = row[0]
            for chunk in chunks:
                cur.execute(
                    """
                    INSERT INTO doc_chunks (page_id, heading_path, chunk_index, content, embedding)
                    VALUES (%s, %s, %s, %s, %s::vector)
                    """,
                    (
                        page_id,
                        chunk["heading_path"],
                        chunk["chunk_index"],
                        chunk["content"],
                        _embedding_literal(chunk["embedding"]),
                    ),
                )
    return len(chunks)


def _delete_missing_pages(conn: psycopg.Connection, source_id: int, seen_urls: set[str]) -> int:
    """Delete `doc_pages` rows for `source_id` whose URL was not seen in this
    crawl (i.e. removed upstream, or unreachable this run)."""
    with conn.cursor() as cur:
        if seen_urls:
            cur.execute(
                "DELETE FROM doc_pages WHERE source_id = %s AND url <> ALL(%s)",
                (source_id, list(seen_urls)),
            )
        else:
            cur.execute("DELETE FROM doc_pages WHERE source_id = %s", (source_id,))
        removed = cur.rowcount
    conn.commit()
    return removed


def _update_source_status(conn: psycopg.Connection, name: str, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE doc_sources SET last_synced = now(), last_status = %s WHERE name = %s",
            (status, name),
        )
    conn.commit()


def sync_source(source: SourceConfig, conn: psycopg.Connection) -> SourceOutcome:
    """Run one full sync of `source` against `conn`. Never raises for
    per-page failures (recorded in the outcome); only a source-level failure
    (e.g. the crawl itself raising) short-circuits with status="failed".
    """
    outcome = SourceOutcome(name=source.name)
    log = logger.bind(source=source.name)

    source_id = ensure_source(conn, source)

    try:
        pages = crawler.crawl(source)
    except Exception as e:  # noqa: BLE001 - any crawl-level failure is source-level
        log.error("crawl_failed", error=str(e))
        outcome.status = "failed"
        outcome.error = str(e)
        _update_source_status(conn, source.name, outcome.status)
        return outcome

    seen_urls: set[str] = set()

    for page in pages:
        url = page["url"]
        html = page["html"]
        seen_urls.add(url)

        try:
            extraction = extract.extract(url, html)
            if extraction.status != "ok":
                outcome.pages_failed += 1
                log.info("page_extract_skipped", url=url, reason=extraction.reason)
                continue

            content_hash = hash_markdown(extraction.markdown)
            existing_hash = get_existing_page_hash(conn, url)
            if existing_hash == content_hash:
                outcome.pages_skipped += 1
                log.info("page_unchanged_skip", url=url)
                continue

            chunks = chunker.chunk_markdown(url, extraction.markdown)
            chunks = embedder.embed_chunks(chunks)
            n = replace_page(conn, source_id, url, content_hash, chunks)
            outcome.pages_fetched += 1
            outcome.chunks_indexed += n
            log.info("page_indexed", url=url, chunks=n, changed=existing_hash is not None)
        except Exception as e:  # noqa: BLE001 - isolate per-page failures
            conn.rollback()
            outcome.pages_failed += 1
            log.error("page_index_failed", url=url, error=str(e))

    outcome.pages_removed = _delete_missing_pages(conn, source_id, seen_urls)

    pages_seen = outcome.pages_fetched + outcome.pages_skipped + outcome.pages_failed
    succeeded_any = outcome.pages_fetched > 0 or outcome.pages_skipped > 0
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

    _update_source_status(conn, source.name, outcome.status)
    log.info(
        "source_sync_complete",
        status=outcome.status,
        pages_fetched=outcome.pages_fetched,
        pages_skipped=outcome.pages_skipped,
        pages_failed=outcome.pages_failed,
        pages_removed=outcome.pages_removed,
        chunks_indexed=outcome.chunks_indexed,
    )
    return outcome


def sync_all(sources: list[SourceConfig]) -> dict[str, SourceOutcome]:
    """Sync each of `sources` in turn, each with its own connection."""
    results: dict[str, SourceOutcome] = {}
    for source in sources:
        conn = get_connection()
        try:
            results[source.name] = sync_source(source, conn)
        finally:
            conn.close()
    return results
