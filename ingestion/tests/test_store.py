"""Integration tests for store.py's hash-diff sync orchestration.

These tests need a live Postgres with the T1 schema applied (the compose
`db` service). They connect using the standard POSTGRES_* env vars and are
skipped automatically if no database is reachable, so `pytest` stays green
in environments without Docker (per-Spoke sandboxes, CI without services).

crawler.crawl / extract.extract are monkeypatched per test so no network
access happens; chunker/embedder run for real (small inputs) to exercise the
full pipeline down to actual `vector` column writes.
"""

from __future__ import annotations

import os

import psycopg
import pytest
from app import store
from app.config import SourceConfig

os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("POSTGRES_PORT", "5433")
os.environ.setdefault("POSTGRES_USER", "self_docs")
os.environ.setdefault("POSTGRES_PASSWORD", "testpass123")
os.environ.setdefault("POSTGRES_DB", "self_docs")


def _db_available() -> bool:
    try:
        conn = store.get_connection()
        conn.close()
        return True
    except psycopg.OperationalError:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="no live Postgres reachable for store.py integration tests")


# Every test in this module creates sources via make_source(), which always
# uses this name unless a test explicitly overrides it. Scoping cleanup to
# these exact names (instead of wiping the whole doc_sources table) keeps
# this suite from ever touching genuine indexed sources (fastapi, traefik,
# docker-compose, pgvector-readme, ...) when run against the live DB.
_TEST_SOURCE_NAMES = ("test-src",)


def _purge_test_sources(c) -> None:
    # doc_pages/doc_chunks cascade from doc_sources via ON DELETE CASCADE, so
    # deleting the source row is sufficient to remove everything it owns.
    c.rollback()  # clear any aborted transaction left by a failing test
    with c.cursor() as cur:
        cur.execute("DELETE FROM doc_sources WHERE name = ANY(%s)", (list(_TEST_SOURCE_NAMES),))
    c.commit()


@pytest.fixture()
def conn():
    c = store.get_connection()
    try:
        _purge_test_sources(c)  # safety net in case a prior run crashed mid-test
        yield c
    finally:
        _purge_test_sources(c)
        c.close()


@pytest.fixture()
def second_conn():
    c = store.get_connection()
    try:
        yield c
    finally:
        c.close()


def make_source(name: str = "test-src", max_pages: int = 10) -> SourceConfig:
    return SourceConfig.model_validate(
        {"name": name, "base_url": "https://example.com/", "max_pages": max_pages}
    )


PAGE_MD = """# Intro

Some intro content that is reasonably long so extraction and chunking behave
normally across several sentences of filler text to reach a sane length for
the tokenizer to chunk into at least one window without tripping any
minimum-length checks in the extraction pipeline logic paths.

## Details

More detail text follows here, again long enough to be meaningful content
for the purposes of this synthetic fixture page used only in tests.
"""


def _fake_crawl_extract(monkeypatch, pages_by_url: dict[str, str]):
    def fake_crawl(source, client=None):
        return [{"url": url, "html": html} for url, html in pages_by_url.items()]

    def fake_extract(url, html):
        from app.extract import ExtractionResult

        return ExtractionResult(url=url, markdown=html, status="ok")

    monkeypatch.setattr(store.crawler, "crawl", fake_crawl)
    monkeypatch.setattr(store.extract, "extract", fake_extract)


def _use_fast_chunk_and_embed(monkeypatch):
    """Replace the real chunker/embedder with trivial fast fakes. Needed for
    tests that seed/sync hundreds of pages (the purge-ratio guard tests) —
    the real fastembed model is far too slow to run at that scale in a unit
    test. `_embedding_literal`/pgvector require exactly `EMBEDDING_DIM`
    (384) floats per chunk."""
    from app.embedder import EMBEDDING_DIM

    def fake_chunk_markdown(url, markdown):
        return [{"heading_path": [], "chunk_index": 0, "content": markdown}]

    def fake_embed_chunks(chunks):
        for c in chunks:
            c["embedding"] = [0.0] * EMBEDDING_DIM
        return chunks

    monkeypatch.setattr(store.chunker, "chunk_markdown", fake_chunk_markdown)
    monkeypatch.setattr(store.embedder, "embed_chunks", fake_embed_chunks)


def test_ensure_source_creates_and_upserts(conn):
    source = make_source()
    sid1 = store.ensure_source(conn, source)
    sid2 = store.ensure_source(conn, source)
    assert sid1 == sid2

    with conn.cursor() as cur:
        cur.execute("SELECT name, base_url FROM doc_sources WHERE id = %s", (sid1,))
        row = cur.fetchone()
    assert row == ("test-src", "https://example.com/")


def test_sync_source_indexes_new_pages_and_second_sync_skips_unchanged(conn, monkeypatch):
    source = make_source()
    _fake_crawl_extract(monkeypatch, {"https://example.com/a": PAGE_MD})

    outcome1 = store.sync_source(source, conn)
    assert outcome1.status == "ok"
    assert outcome1.pages_fetched == 1
    assert outcome1.pages_skipped == 0
    assert outcome1.chunks_indexed > 0

    def _chunk_count_for_source(name: str) -> int:
        # Scoped to this test's own source: the live DB also holds the real
        # indexed corpus (thousands of unrelated chunks), so an unscoped
        # `count(*)` would assert against the wrong number.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM doc_chunks c
                JOIN doc_pages p ON p.id = c.page_id
                JOIN doc_sources s ON s.id = p.source_id
                WHERE s.name = %s
                """,
                (name,),
            )
            (n,) = cur.fetchone()
        return n

    chunk_count = _chunk_count_for_source(source.name)
    assert chunk_count == outcome1.chunks_indexed

    # Second sync of the *same* content must skip the unchanged page entirely.
    outcome2 = store.sync_source(source, conn)
    assert outcome2.status == "ok"
    assert outcome2.pages_fetched == 0
    assert outcome2.pages_skipped == 1
    assert outcome2.chunks_indexed == 0

    chunk_count_after = _chunk_count_for_source(source.name)
    assert chunk_count_after == chunk_count  # untouched


def test_changed_page_is_reembedded_others_untouched(conn, monkeypatch):
    source = make_source()
    _fake_crawl_extract(
        monkeypatch,
        {"https://example.com/a": PAGE_MD, "https://example.com/b": PAGE_MD.replace("Intro", "Intro B")},
    )
    outcome1 = store.sync_source(source, conn)
    assert outcome1.pages_fetched == 2

    with conn.cursor() as cur:
        cur.execute("SELECT id, content_hash FROM doc_pages WHERE url = %s", ("https://example.com/a",))
        page_a_id, hash_before = cur.fetchone()

    # Mutate only page b's content; page a stays identical.
    _fake_crawl_extract(
        monkeypatch,
        {
            "https://example.com/a": PAGE_MD,
            "https://example.com/b": PAGE_MD.replace("Intro", "Intro B changed now") + "\nextra paragraph text here.",
        },
    )
    outcome2 = store.sync_source(source, conn)
    assert outcome2.pages_fetched == 1  # only b re-embedded
    assert outcome2.pages_skipped == 1  # a skipped

    with conn.cursor() as cur:
        cur.execute("SELECT id, content_hash FROM doc_pages WHERE url = %s", ("https://example.com/a",))
        page_a_id_after, hash_after = cur.fetchone()
    assert page_a_id_after == page_a_id
    assert hash_after == hash_before


def test_pages_removed_upstream_are_deleted(conn, second_conn, monkeypatch):
    source = make_source()
    _fake_crawl_extract(
        monkeypatch,
        {"https://example.com/a": PAGE_MD, "https://example.com/b": PAGE_MD.replace("Intro", "Intro B")},
    )
    store.sync_source(source, conn)

    with conn.cursor() as cur:
        # Scoped to this test's own source (see note in the previous test) —
        # the live DB also holds the real indexed corpus's pages.
        cur.execute(
            "SELECT count(*) FROM doc_pages p JOIN doc_sources s ON s.id = p.source_id WHERE s.name = %s",
            (source.name,),
        )
        (count_before,) = cur.fetchone()
    assert count_before == 2

    # Next crawl only returns page a — page b was removed upstream.
    _fake_crawl_extract(monkeypatch, {"https://example.com/a": PAGE_MD})
    outcome = store.sync_source(source, conn)
    assert outcome.pages_removed == 1

    with second_conn.cursor() as cur:
        cur.execute(
            "SELECT url FROM doc_pages p JOIN doc_sources s ON s.id = p.source_id WHERE s.name = %s",
            (source.name,),
        )
        urls = {r[0] for r in cur.fetchall()}
    assert urls == {"https://example.com/a"}


def test_source_status_failed_on_crawl_error(conn, monkeypatch):
    source = make_source()

    def raising_crawl(source, client=None):
        raise RuntimeError("sitemap host is dead")

    monkeypatch.setattr(store.crawler, "crawl", raising_crawl)

    outcome = store.sync_source(source, conn)
    assert outcome.status == "failed"
    assert "sitemap host is dead" in outcome.error

    with conn.cursor() as cur:
        cur.execute("SELECT last_status FROM doc_sources WHERE name = %s", (source.name,))
        (status,) = cur.fetchone()
    assert status == "failed"


def _seed_pages(conn, monkeypatch, source: SourceConfig, urls: list[str]) -> None:
    """Run one clean sync that indexes `urls`, simulating a pre-existing
    corpus the next (aborted) sync must not prune."""
    _fake_crawl_extract(monkeypatch, {u: PAGE_MD for u in urls})
    outcome = store.sync_source(source, conn)
    assert outcome.status == "ok"
    assert outcome.pages_fetched == len(urls)


def _existing_urls(conn, source_name: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.url FROM doc_pages p JOIN doc_sources s ON s.id = p.source_id WHERE s.name = %s",
            (source_name,),
        )
        return {r[0] for r in cur.fetchall()}


def test_crawl_failure_mid_iteration_keeps_already_committed_pages(conn, monkeypatch):
    """Regression test for the incident this task fixes: `crawl()` used to
    materialize the whole crawl before any DB write, so a connection dropped
    mid-crawl (e.g. "the connection is lost") lost every page already
    fetched. With `crawl()` as a generator, pages are committed as they're
    yielded, so a crash partway through must still leave earlier pages in
    `doc_pages`."""
    source = make_source()

    def fake_crawl(source, client=None):
        yield {"url": "https://example.com/good", "html": PAGE_MD}
        raise RuntimeError("the connection is lost")

    def fake_extract(url, html):
        from app.extract import ExtractionResult

        return ExtractionResult(url=url, markdown=html, status="ok")

    monkeypatch.setattr(store.crawler, "crawl", fake_crawl)
    monkeypatch.setattr(store.extract, "extract", fake_extract)

    outcome = store.sync_source(source, conn)
    assert outcome.pages_fetched == 1
    assert outcome.error and "connection is lost" in outcome.error
    # A crawl that didn't finish must never be reported fully "ok".
    assert outcome.status != "ok"

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM doc_pages WHERE url = %s", ("https://example.com/good",))
        (n,) = cur.fetchone()
    assert n == 1

    with conn.cursor() as cur:
        cur.execute("SELECT last_status FROM doc_sources WHERE name = %s", (source.name,))
        (status,) = cur.fetchone()
    assert status is not None and status != "ok"


def test_mid_crawl_abort_does_not_prune_pages_it_never_reached(conn, second_conn, monkeypatch):
    """The stale-page purge (`_delete_missing_pages`) only deletes pages
    absent from `seen_urls`. On an aborted crawl, `seen_urls` is a partial
    enumeration, not "the current truth" — running the purge against it
    would wipe every legitimate page the crawl hadn't gotten to yet. This
    must never happen: an aborted sync should leave the existing corpus
    fully intact and report pages_removed == 0."""
    source = make_source()
    existing_urls = [f"https://example.com/page-{i}" for i in range(10)]
    _seed_pages(conn, monkeypatch, source, existing_urls)
    assert _existing_urls(conn, source.name) == set(existing_urls)

    def fake_crawl(source, client=None):
        yield {"url": "https://example.com/page-0", "html": PAGE_MD}
        raise RuntimeError("the connection is lost")

    def fake_extract(url, html):
        from app.extract import ExtractionResult

        return ExtractionResult(url=url, markdown=html, status="ok")

    monkeypatch.setattr(store.crawler, "crawl", fake_crawl)
    monkeypatch.setattr(store.extract, "extract", fake_extract)

    outcome = store.sync_source(source, conn)

    assert outcome.pages_removed == 0
    # The other 9 pages the aborted crawl never reached must still be present.
    assert _existing_urls(second_conn, source.name) == set(existing_urls)


def test_mid_crawl_abort_on_very_first_page_does_not_wipe_source(conn, second_conn, monkeypatch):
    """The `seen_urls` empty case is the worst-case version of the above: an
    abort before yielding a single page must not be treated as "this source
    now has zero pages" — `_delete_missing_pages` deletes everything for the
    source when `seen_urls` is empty, so this path must be skipped entirely."""
    source = make_source()
    existing_urls = [f"https://example.com/page-{i}" for i in range(5)]
    _seed_pages(conn, monkeypatch, source, existing_urls)

    def fake_crawl(source, client=None):
        raise RuntimeError("the connection is lost")
        yield  # pragma: no cover - makes this a generator function

    monkeypatch.setattr(store.crawler, "crawl", fake_crawl)

    outcome = store.sync_source(source, conn)

    assert outcome.pages_removed == 0
    assert _existing_urls(second_conn, source.name) == set(existing_urls)


def test_completed_but_empty_crawl_does_not_wipe_source(conn, second_conn, monkeypatch):
    """Critical regression test: a crawl that runs to COMPLETION (reaches
    StopIteration normally, no exception) but yields zero pages must not be
    treated as "this source now has zero pages upstream." This happens for
    entirely mundane reasons — e.g. a sitemap URL 5xx's or times out, the
    crawler swallows it into BFS fallback, and every candidate URL then
    fails its fetch — with no exception ever raised. `crawl_aborted_early`
    is False in this case, so the purge guard must key off `seen_urls` being
    empty too, independent of whether the crawl "failed" or just legitimately
    found nothing this run."""
    source = make_source()
    existing_urls = [f"https://example.com/page-{i}" for i in range(6)]
    _seed_pages(conn, monkeypatch, source, existing_urls)

    def empty_but_completed_crawl(source, client=None):
        return iter([])  # a real, exhausted iterator - StopIteration on first next()

    monkeypatch.setattr(store.crawler, "crawl", empty_but_completed_crawl)

    outcome = store.sync_source(source, conn)

    assert outcome.pages_removed == 0
    assert outcome.status == "failed"  # correctly flagged - but the corpus must survive
    assert _existing_urls(second_conn, source.name) == set(existing_urls)


def test_delete_missing_pages_refuses_empty_seen_urls_by_default(conn, second_conn, monkeypatch):
    """`_delete_missing_pages` must itself be safe to call directly with an
    empty `seen_urls` — defense in depth independent of any caller-side
    guard in `sync_source`."""
    source = make_source()
    existing_urls = [f"https://example.com/page-{i}" for i in range(3)]
    _seed_pages(conn, monkeypatch, source, existing_urls)

    with conn.cursor() as cur:
        cur.execute("SELECT id FROM doc_sources WHERE name = %s", (source.name,))
        (source_id,) = cur.fetchone()

    removed = store._delete_missing_pages(conn, source_id, set())

    assert removed == 0
    assert _existing_urls(second_conn, source.name) == set(existing_urls)

    # The explicit opt-in still works, for a genuine "wipe this source" op.
    removed_forced = store._delete_missing_pages(conn, source_id, set(), force_delete_all=True)
    assert removed_forced == len(existing_urls)
    assert _existing_urls(second_conn, source.name) == set()


def test_purge_ratio_guard_refuses_bfs_collapse_real_traefik_numbers(conn, second_conn, monkeypatch):
    """Real-numbers regression test for the BFS-collapse purge scenario:
    397 existing pages (traefik's actual live doc_pages count), a completed
    crawl that only reaches 60 of them (silent sitemap-fetch failure ->
    link-graph BFS fallback, which routinely covers a small fraction of a
    sitemap's reach). coverage = 60/397 = 0.151 (< the 0.3 floor), delete
    ratio = 337/397 = 0.849 (> the 0.5 threshold) -> both signals trip ->
    the purge MUST be refused and the existing corpus left untouched."""
    source = make_source(max_pages=500)
    _use_fast_chunk_and_embed(monkeypatch)

    existing_urls = [f"https://example.com/page-{i}" for i in range(397)]
    _seed_pages(conn, monkeypatch, source, existing_urls)

    # BFS collapse: the crawl only reaches a small connected subset.
    bfs_reached = existing_urls[:60]
    _fake_crawl_extract(monkeypatch, {u: PAGE_MD for u in bfs_reached})

    outcome = store.sync_source(source, conn)

    assert outcome.pages_removed == 0
    assert _existing_urls(second_conn, source.name) == set(existing_urls)


def test_purge_ratio_guard_permits_traefik_style_self_heal_real_numbers(conn, second_conn, monkeypatch):
    """Real-numbers regression test for the traefik repair this guard exists
    to still allow: 397 existing pages, 165 legitimately in-scope
    (`/traefik/...`) and 232 wrong-product (`/traefik-hub/...`) that a
    corrected `include_prefixes` filter now excludes. The corrected re-crawl
    fetches 280 in-scope pages (the 165 that already existed plus 115 newly
    discovered ones) -> coverage = 280/397 = 0.705 (comfortably clears the
    0.3 floor), delete ratio = 232/397 = 0.584 (over the 0.5 threshold, but
    the healthy coverage signal permits it anyway) -> the purge of the 232
    wrong-product pages MUST proceed."""
    source = make_source(max_pages=500)
    _use_fast_chunk_and_embed(monkeypatch)

    in_scope_existing = [f"https://example.com/traefik/page-{i}" for i in range(165)]
    wrong_product_existing = [f"https://example.com/traefik-hub/page-{i}" for i in range(232)]
    existing_urls = in_scope_existing + wrong_product_existing
    assert len(existing_urls) == 397
    _seed_pages(conn, monkeypatch, source, existing_urls)

    # Corrected crawl (include_prefixes now scopes to /traefik/ only): the
    # 165 previously-seen in-scope pages plus 115 newly-discovered ones.
    corrected_urls = [f"https://example.com/traefik/page-{i}" for i in range(280)]
    _fake_crawl_extract(monkeypatch, {u: PAGE_MD for u in corrected_urls})

    outcome = store.sync_source(source, conn)

    assert outcome.pages_removed == 232
    remaining = _existing_urls(second_conn, source.name)
    assert remaining == set(corrected_urls)
    assert not any("traefik-hub" in u for u in remaining)


def test_mark_source_failed_persists_status_on_fresh_connection(conn):
    source = make_source()
    store.ensure_source(conn, source)
    conn.commit()

    store.mark_source_failed(source.name)

    with conn.cursor() as cur:
        cur.execute("SELECT last_status FROM doc_sources WHERE name = %s", (source.name,))
        (status,) = cur.fetchone()
    assert status == "failed"


def test_source_status_ok_on_soft_page_failures(conn, monkeypatch):
    # If some pages encounter soft failures (e.g. skipped extract or fetch failure)
    # while others succeed, status should remain "ok" and pages_soft_failed incremented.
    source = make_source()

    def fake_crawl(source, client=None):
        return [
            {"url": "https://example.com/a", "html": "x"},
            {"url": "https://example.com/b", "html": "longer content"},
        ]

    from app.extract import ExtractionResult

    def fake_extract(url, html):
        if html == "x":
            return ExtractionResult(url=url, markdown=None, status="skipped", reason="too short")
        return ExtractionResult(url=url, markdown=html, status="ok")

    monkeypatch.setattr(store.crawler, "crawl", fake_crawl)
    monkeypatch.setattr(store.extract, "extract", fake_extract)

    outcome = store.sync_source(source, conn)
    assert outcome.status == "ok"
    assert outcome.pages_fetched == 1
    assert outcome.pages_soft_failed == 1
    assert outcome.pages_failed == 0


def test_source_status_partial_on_hard_page_failures(conn, monkeypatch):
    # If some pages encounter real hard pipeline exceptions (e.g. DB or chunker errors)
    # while others succeed, status MUST be "partial" and pages_failed incremented.
    source = make_source()

    def fake_crawl(source, client=None):
        return [
            {"url": "https://example.com/good", "html": "good content"},
            {"url": "https://example.com/bad", "html": "bad content"},
        ]

    from app.extract import ExtractionResult

    def fake_extract(url, html):
        return ExtractionResult(url=url, markdown=html, status="ok")

    orig_replace = store.replace_page

    def fake_replace_page(conn_arg, source_id, url, content_hash, chunks, **kwargs):
        if url == "https://example.com/bad":
            raise RuntimeError("hard DB write error")
        return orig_replace(conn_arg, source_id, url, content_hash, chunks, **kwargs)

    monkeypatch.setattr(store.crawler, "crawl", fake_crawl)
    monkeypatch.setattr(store.extract, "extract", fake_extract)
    monkeypatch.setattr(store, "replace_page", fake_replace_page)

    outcome = store.sync_source(source, conn)
    assert outcome.status == "partial"
    assert outcome.pages_fetched == 1
    assert outcome.pages_failed == 1
    assert outcome.pages_soft_failed == 0


def test_source_status_failed_on_empty_crawl(conn, monkeypatch):
    # A crawl that fetches/skips/fails nothing (e.g. every candidate URL was
    # filtered out before the first fetch) must never report "ok" — that
    # would silently hide a source indexing 0 pages from partial/failed
    # alerting.
    source = make_source()

    def empty_crawl(source, client=None):
        return []

    monkeypatch.setattr(store.crawler, "crawl", empty_crawl)

    outcome = store.sync_source(source, conn)
    assert outcome.pages_fetched == 0
    assert outcome.pages_skipped == 0
    assert outcome.pages_failed == 0
    assert outcome.status == "failed"

    with conn.cursor() as cur:
        cur.execute("SELECT last_status FROM doc_sources WHERE name = %s", (source.name,))
        (status,) = cur.fetchone()
    assert status == "failed"


def test_sync_source_durability_visible_from_second_connection(conn, monkeypatch):
    """Verify that rows written via sync_source are immediately visible from a
    second, independent database connection without waiting for the sync connection
    to close."""
    source = make_source()
    _fake_crawl_extract(monkeypatch, {"https://example.com/durability": PAGE_MD})

    outcome = store.sync_source(source, conn)
    assert outcome.status == "ok"
    assert outcome.pages_fetched == 1

    second_conn = store.get_connection()
    try:
        with second_conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM doc_pages p
                JOIN doc_sources s ON s.id = p.source_id
                WHERE s.name = %s AND p.url = %s
                """,
                (source.name, "https://example.com/durability"),
            )
            (n_pages,) = cur.fetchone()
            assert n_pages == 1

            cur.execute(
                """
                SELECT count(*) FROM doc_chunks c
                JOIN doc_pages p ON p.id = c.page_id
                JOIN doc_sources s ON s.id = p.source_id
                WHERE s.name = %s
                """,
                (source.name,),
            )
            (n_chunks,) = cur.fetchone()
            assert n_chunks == outcome.chunks_indexed
    finally:
        second_conn.close()


def test_crash_after_n_pages_leaves_exactly_n_pages_durable_verified_cross_connection(conn, monkeypatch):
    """Verify that when a crawl crashes after yielding N pages, exactly those N
    pages have already been committed and are durable when checked from a second,
    independent connection."""
    source = make_source()

    def fake_crawl(source, client=None):
        yield {"url": "https://example.com/page1", "html": PAGE_MD}
        yield {"url": "https://example.com/page2", "html": PAGE_MD}
        raise RuntimeError("network failure after 2 pages")

    def fake_extract(url, html):
        from app.extract import ExtractionResult

        return ExtractionResult(url=url, markdown=html, status="ok")

    monkeypatch.setattr(store.crawler, "crawl", fake_crawl)
    monkeypatch.setattr(store.extract, "extract", fake_extract)

    outcome = store.sync_source(source, conn)
    assert outcome.pages_fetched == 2
    assert outcome.status != "ok"

    second_conn = store.get_connection()
    try:
        with second_conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.url FROM doc_pages p
                JOIN doc_sources s ON s.id = p.source_id
                WHERE s.name = %s
                ORDER BY p.url
                """,
                (source.name,),
            )
            urls = [r[0] for r in cur.fetchall()]
            assert urls == ["https://example.com/page1", "https://example.com/page2"]
    finally:
        second_conn.close()


def test_replace_page_atomic_no_partial_chunks_on_failure(conn, monkeypatch):
    """Verify replace_page remains atomic per page: if chunk insertion fails partway
    through, the entire replace_page transaction rolls back, leaving no partial chunks
    and preserving the old page state. Verified cross-connection."""
    source = make_source()
    _fake_crawl_extract(monkeypatch, {"https://example.com/atomic": PAGE_MD})
    outcome = store.sync_source(source, conn)
    assert outcome.pages_fetched == 1

    with conn.cursor() as cur:
        cur.execute("SELECT id FROM doc_sources WHERE name = %s", (source.name,))
        (source_id,) = cur.fetchone()

    from app.embedder import EMBEDDING_DIM

    bad_chunks = [
        {"heading_path": ["H1"], "chunk_index": 0, "content": "Chunk 0 good", "embedding": [0.1] * EMBEDDING_DIM},
        {"heading_path": ["H2"], "chunk_index": 1, "content": "Chunk 1 bad", "embedding": [0.1] * 10},
    ]

    with pytest.raises(psycopg.Error):
        store.replace_page(conn, source_id, "https://example.com/atomic", "new_hash", bad_chunks)

    second_conn = store.get_connection()
    try:
        with second_conn.cursor() as cur:
            cur.execute(
                "SELECT content_hash FROM doc_pages WHERE url = %s",
                ("https://example.com/atomic",),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] != "new_hash"

            cur.execute(
                """
                SELECT count(*) FROM doc_chunks c
                JOIN doc_pages p ON p.id = c.page_id
                JOIN doc_sources s ON s.id = p.source_id
                WHERE s.name = %s
                """,
                (source.name,),
            )
            (n_chunks,) = cur.fetchone()
            assert n_chunks == outcome.chunks_indexed
    finally:
        second_conn.close()


def test_no_idle_in_transaction_during_multi_page_sync(conn, monkeypatch):
    """Verify that during a multi-page crawl and sync, the database connection
    is never left in an 'idle in transaction' state while yielding pages."""
    source = make_source()

    observed_states = []

    def fake_crawl(source, client=None):
        yield {"url": "https://example.com/p1", "html": PAGE_MD}
        second_conn = store.get_connection()
        try:
            with second_conn.cursor() as cur:
                cur.execute(
                    "SELECT state FROM pg_stat_activity WHERE pid = %s",
                    (conn.info.backend_pid,),
                )
                row = cur.fetchone()
                if row:
                    observed_states.append(row[0])
        finally:
            second_conn.close()
        yield {"url": "https://example.com/p2", "html": PAGE_MD}

    def fake_extract(url, html):
        from app.extract import ExtractionResult

        return ExtractionResult(url=url, markdown=html, status="ok")

    monkeypatch.setattr(store.crawler, "crawl", fake_crawl)
    monkeypatch.setattr(store.extract, "extract", fake_extract)

    outcome = store.sync_source(source, conn)
    assert outcome.pages_fetched == 2
    assert len(observed_states) == 1
    assert observed_states[0] != "idle in transaction"
    assert observed_states[0] == "idle"


def test_sync_source_fetch_failed_503_preserves_existing_page_verified_second_conn(conn, monkeypatch):
    """An existing page whose fetch 503s (`fetch_ok=False`) during a sync must NOT
    be deleted by `_delete_missing_pages`, and its chunks survive, verified from
    a second connection."""
    source = make_source()

    def fake_crawl_first(source, client=None):
        yield {"url": "https://example.com/p1", "html": PAGE_MD, "fetch_ok": True}

    def fake_extract(url, html):
        from app.extract import ExtractionResult
        return ExtractionResult(url=url, markdown=html, status="ok")

    monkeypatch.setattr(store.crawler, "crawl", fake_crawl_first)
    monkeypatch.setattr(store.extract, "extract", fake_extract)

    outcome_first = store.sync_source(source, conn)
    assert outcome_first.pages_fetched == 1
    assert outcome_first.chunks_indexed > 0

    # Second sync where the page fails to fetch (503 / fetch_ok=False)
    def fake_crawl_second(source, client=None):
        yield {"url": "https://example.com/p1", "html": None, "fetch_ok": False}

    monkeypatch.setattr(store.crawler, "crawl", fake_crawl_second)
    outcome_second = store.sync_source(source, conn)
    assert outcome_second.pages_soft_failed == 1
    assert outcome_second.pages_failed == 0
    assert outcome_second.pages_removed == 0
    assert outcome_second.status == "ok"

    second_conn = store.get_connection()
    try:
        with second_conn.cursor() as cur:
            cur.execute("SELECT url FROM doc_pages WHERE url = %s", ("https://example.com/p1",))
            assert cur.fetchone() is not None

            cur.execute(
                """
                SELECT count(*) FROM doc_chunks c
                JOIN doc_pages p ON p.id = c.page_id
                JOIN doc_sources s ON s.id = p.source_id
                WHERE s.name = %s
                """,
                (source.name,),
            )
            (n_chunks,) = cur.fetchone()
            assert n_chunks == outcome_first.chunks_indexed
    finally:
        second_conn.close()


def test_sync_source_genuinely_absent_page_is_still_purged(conn, monkeypatch):
    """Verify that a page genuinely absent from the crawl (not yielded at all) IS
    still purged by `_delete_missing_pages`."""
    source = make_source()

    def fake_crawl_first(source, client=None):
        yield {"url": "https://example.com/p1", "html": PAGE_MD, "fetch_ok": True}
        yield {"url": "https://example.com/p2", "html": PAGE_MD, "fetch_ok": True}

    def fake_extract(url, html):
        from app.extract import ExtractionResult
        return ExtractionResult(url=url, markdown=html, status="ok")

    monkeypatch.setattr(store.crawler, "crawl", fake_crawl_first)
    monkeypatch.setattr(store.extract, "extract", fake_extract)

    outcome_first = store.sync_source(source, conn)
    assert outcome_first.pages_fetched == 2

    # Second sync where p2 is genuinely gone (neither fetch_ok=True nor fetch_ok=False)
    def fake_crawl_second(source, client=None):
        yield {"url": "https://example.com/p1", "html": PAGE_MD, "fetch_ok": True}

    monkeypatch.setattr(store.crawler, "crawl", fake_crawl_second)
    outcome_second = store.sync_source(source, conn)
    assert outcome_second.pages_removed == 1

    second_conn = store.get_connection()
    try:
        with second_conn.cursor() as cur:
            cur.execute("SELECT url FROM doc_pages WHERE url = %s", ("https://example.com/p2",))
            assert cur.fetchone() is None
            cur.execute("SELECT url FROM doc_pages WHERE url = %s", ("https://example.com/p1",))
            assert cur.fetchone() is not None
    finally:
        second_conn.close()


def test_sync_source_all_pages_soft_failed_reports_status_ok(conn, monkeypatch):
    """A source whose pages all soft-fail (e.g. 404/503 fetch errors) reports status 'ok' and tracks soft failures."""
    source = make_source()

    def fake_crawl(source, client=None):
        yield {"url": "https://example.com/broken1", "html": None, "fetch_ok": False}
        yield {"url": "https://example.com/broken2", "html": None, "fetch_ok": False}

    monkeypatch.setattr(store.crawler, "crawl", fake_crawl)

    outcome = store.sync_source(source, conn)
    assert outcome.pages_soft_failed == 2
    assert outcome.pages_failed == 0
    assert outcome.pages_fetched == 0
    assert outcome.status == "ok"

    second_conn = store.get_connection()
    try:
        with second_conn.cursor() as cur:
            cur.execute("SELECT last_status FROM doc_sources WHERE name = %s", (source.name,))
            assert cur.fetchone()[0] == "ok"
    finally:
        second_conn.close()


def test_purge_ratio_guard_computed_against_pre_sync_snapshot(conn, second_conn, monkeypatch):
    """Prove that `_delete_missing_pages` computes delete_ratio and coverage_ratio
    against the pre-sync existing_count snapshot rather than the post-loop snapshot
    inflated by newly inserted pages during an autocommit run."""
    source = make_source(max_pages=500)
    _use_fast_chunk_and_embed(monkeypatch)

    existing_urls = [f"https://example.com/page-{i}" for i in range(397)]
    _seed_pages(conn, monkeypatch, source, existing_urls)

    # We discover 125 of the old existing pages, plus 100 brand new pages.
    # Against pre-sync existing_count = 397:
    #   would_delete = 397 - 125 = 272 -> delete_ratio = 272/397 = 0.685 (> 0.5 threshold)
    #   successful_seen = 125 -> coverage_ratio = 125/397 = 0.315 (clears 0.3 floor)
    # So against the intended pre-sync snapshot, the purge is PERMITTED.
    #
    # If evaluated against post-loop existing_count = 397 + 100 = 497:
    #   coverage_ratio = 125/497 = 0.252 (< 0.3 floor), which would REFUSE the purge!
    old_seen = existing_urls[:125]
    new_seen = [f"https://example.com/new-page-{i}" for i in range(100)]
    _fake_crawl_extract(monkeypatch, {u: PAGE_MD for u in old_seen + new_seen})

    outcome = store.sync_source(source, conn)

    # Because pre-sync snapshot (397) is used, coverage (125/397 >= 0.3) permits purge.
    assert outcome.pages_removed == 272
    assert len(_existing_urls(second_conn, source.name)) == 225  # 125 old + 100 new


def test_purge_ratio_guard_excludes_fetch_failures_from_coverage_ratio(conn, second_conn, monkeypatch):
    """Prove that fetch-failed URLs (fetch_ok=False) added to seen_urls by S3 do NOT
    count toward coverage_ratio, preventing a mass-fetch-failure run with high failure
    counts from clearing the 0.3 coverage floor and wiping the existing corpus."""
    source = make_source(max_pages=500)
    _use_fast_chunk_and_embed(monkeypatch)

    existing_urls = [f"https://example.com/page-{i}" for i in range(397)]
    _seed_pages(conn, monkeypatch, source, existing_urls)

    # A mass-fetch-failure run: 150 URLs attempted but all fail (fetch_ok=False), 0 successes.
    # If fetch failures counted toward coverage_ratio:
    #   coverage = 150 / 397 = 0.378 (clears 0.3 floor!), delete_ratio = 397/397 = 1.0 (> 0.5)
    #   -> would allow wiping the entire 397-page corpus!
    # With successful_seen_count excluding fetch failures:
    #   coverage = 0 / 397 = 0.0 (< 0.3 floor) -> REFUSED by guard.
    def fake_crawl(source, client=None):
        for i in range(150):
            yield {"url": f"https://example.com/broken-{i}", "html": None, "fetch_ok": False}

    monkeypatch.setattr(store.crawler, "crawl", fake_crawl)

    outcome = store.sync_source(source, conn)

    assert outcome.pages_soft_failed == 150
    assert outcome.pages_failed == 0
    assert outcome.pages_removed == 0
    assert len(_existing_urls(second_conn, source.name)) == 397


def test_recovery_page_extract_failed_preserves_existing_row_and_continues(conn, second_conn, monkeypatch):
    """(B2) If a page yields but extraction fails (status != 'ok'), existing row
    and chunks survive, pages_failed is incremented, and sync continues to subsequent pages."""
    source = make_source()
    _seed_pages(conn, monkeypatch, source, ["https://example.com/p1", "https://example.com/p2"])
    assert len(_existing_urls(second_conn, source.name)) == 2

    def fake_crawl(source, client=None):
        yield {"url": "https://example.com/p1", "html": "bad html"}
        yield {"url": "https://example.com/p2", "html": PAGE_MD}

    def fake_extract(url, html):
        from app.extract import ExtractionResult
        if url == "https://example.com/p1":
            return ExtractionResult(url=url, markdown="", status="error", reason="malformed HTML")
        return ExtractionResult(url=url, markdown=html, status="ok")

    monkeypatch.setattr(store.crawler, "crawl", fake_crawl)
    monkeypatch.setattr(store.extract, "extract", fake_extract)

    outcome = store.sync_source(source, conn)
    assert outcome.pages_soft_failed == 1
    assert outcome.pages_failed == 0
    assert outcome.pages_fetched == 1 or outcome.pages_skipped == 1

    urls = _existing_urls(second_conn, source.name)
    assert urls == {"https://example.com/p1", "https://example.com/p2"}


def test_recovery_replace_page_raises_preserves_existing_and_continues(conn, second_conn, monkeypatch):
    """(B3) If replace_page raises an exception mid-sync, no partial chunks are left
    for that page, the previous good version of the row is intact, and sync continues
    to subsequent pages."""
    source = make_source()
    _seed_pages(conn, monkeypatch, source, ["https://example.com/p1", "https://example.com/p2"])

    with second_conn.cursor() as cur:
        cur.execute("SELECT content_hash FROM doc_pages WHERE url = %s", ("https://example.com/p1",))
        (old_hash,) = cur.fetchone()

    real_replace_page = store.replace_page
    def fake_replace_page(c, source_id, url, content_hash, chunks):
        if url == "https://example.com/p1":
            raise psycopg.OperationalError("simulated database failure on replace_page")
        return real_replace_page(c, source_id, url, content_hash, chunks)

    monkeypatch.setattr(store, "replace_page", fake_replace_page)

    _fake_crawl_extract(monkeypatch, {
        "https://example.com/p1": PAGE_MD.replace("Intro", "Intro Modified"),
        "https://example.com/p2": PAGE_MD
    })

    outcome = store.sync_source(source, conn)
    assert outcome.pages_failed == 1

    with second_conn.cursor() as cur:
        cur.execute("SELECT content_hash FROM doc_pages WHERE url = %s", ("https://example.com/p1",))
        (current_hash,) = cur.fetchone()
        assert current_hash == old_hash


def test_recovery_mixed_source_partial_status_no_purge(conn, second_conn, monkeypatch):
    """(B4) Mixed source: successes are durable, failures (fetch and extract) leave
    prior content intact, status is 'partial' not 'ok', and NOTHING is purged."""
    source = make_source()
    _seed_pages(conn, monkeypatch, source, [
        "https://example.com/good",
        "https://example.com/fetch_fail",
        "https://example.com/extract_fail"
    ])

    def fake_crawl(source, client=None):
        yield {"url": "https://example.com/good", "html": PAGE_MD}
        yield {"url": "https://example.com/fetch_fail", "html": None, "fetch_ok": False}
        yield {"url": "https://example.com/extract_fail", "html": "bad html"}

    def fake_extract(url, html):
        from app.extract import ExtractionResult
        if url == "https://example.com/extract_fail":
            return ExtractionResult(url=url, markdown="", status="error", reason="extraction error")
        return ExtractionResult(url=url, markdown=html, status="ok")

    monkeypatch.setattr(store.crawler, "crawl", fake_crawl)
    monkeypatch.setattr(store.extract, "extract", fake_extract)

    outcome = store.sync_source(source, conn)
    assert outcome.status == "ok"
    assert outcome.pages_fetched == 1 or outcome.pages_skipped == 1
    assert outcome.pages_soft_failed == 2
    assert outcome.pages_failed == 0
    assert outcome.pages_removed == 0

    urls = _existing_urls(second_conn, source.name)
    assert urls == {
        "https://example.com/good",
        "https://example.com/fetch_fail",
        "https://example.com/extract_fail"
    }


def test_recovery_resume_across_syncs_incremental(conn, second_conn, monkeypatch):
    """(B5) Resume across syncs: after a run where page X failed, a second sync retries X
    and ends with X present and correct; already-successful pages are hash-skipped on
    the second run."""
    source = make_source()

    def crawl_run1(source, client=None):
        yield {"url": "https://example.com/p1", "html": PAGE_MD}
        yield {"url": "https://example.com/p2", "html": None, "fetch_ok": False}

    def fake_extract(url, html):
        from app.extract import ExtractionResult
        return ExtractionResult(url=url, markdown=html, status="ok")

    monkeypatch.setattr(store.crawler, "crawl", crawl_run1)
    monkeypatch.setattr(store.extract, "extract", fake_extract)

    outcome1 = store.sync_source(source, conn)
    assert outcome1.pages_fetched == 1
    assert outcome1.pages_soft_failed == 1
    assert outcome1.pages_failed == 0
    assert _existing_urls(second_conn, source.name) == {"https://example.com/p1"}

    def crawl_run2(source, client=None):
        yield {"url": "https://example.com/p1", "html": PAGE_MD}
        yield {"url": "https://example.com/p2", "html": PAGE_MD}

    monkeypatch.setattr(store.crawler, "crawl", crawl_run2)
    outcome2 = store.sync_source(source, conn)
    assert outcome2.pages_skipped == 1  # p1 skipped by hash!
    assert outcome2.pages_fetched == 1  # p2 indexed!
    assert outcome2.status == "ok"
    assert _existing_urls(second_conn, source.name) == {"https://example.com/p1", "https://example.com/p2"}


def _fake_crawl_items(monkeypatch, items):
    """Install a crawler.crawl double that yields `items` verbatim (already
    in the shape `crawl()` would yield: markdown items, not_modified items,
    or an llms_index_unchanged sentinel). Accepts **kwargs (in particular
    `conditional=`) since `sync_source` calls `crawler.crawl(source,
    conditional=...)` first, falling back to `crawl(source)` only on
    TypeError."""

    def fake_crawl(source, client=None, conditional=None, **kwargs):
        yield from items

    monkeypatch.setattr(store.crawler, "crawl", fake_crawl)


def test_sync_source_markdown_item_indexes_with_source_language_fts_config(conn, monkeypatch):
    """A crawl yielding a 'markdown' item (llms.txt fast-path) must be
    indexed without going through extract.extract, and every inserted
    doc_chunks row's fts_config must equal source.language."""
    source = SourceConfig.model_validate(
        {"name": "test-src", "base_url": "https://example.com/", "max_pages": 10, "language": "french"}
    )
    _use_fast_chunk_and_embed(monkeypatch)
    _fake_crawl_items(
        monkeypatch,
        [
            {
                "url": "https://example.com/fr-page",
                "markdown": "# Titre\nCeci est un contenu de test en francais pour la config fts.",
                "heading_path": "Titre",
                "fetch_ok": True,
            }
        ],
    )

    outcome = store.sync_source(source, conn)
    assert outcome.status == "ok"
    assert outcome.pages_fetched == 1
    assert outcome.chunks_indexed > 0

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.fts_config::text FROM doc_chunks c
            JOIN doc_pages p ON p.id = c.page_id
            JOIN doc_sources s ON s.id = p.source_id
            WHERE s.name = %s
            """,
            (source.name,),
        )
        rows = cur.fetchall()
    assert rows
    assert all(r[0] == "french" for r in rows)


def test_sync_source_per_page_not_modified_bumps_outcome_and_leaves_row_untouched(conn, monkeypatch):
    """A per-page 304 (`not_modified: True`, no markdown/html) must bump
    outcome.pages_not_modified and leave the existing doc_pages row exactly
    as-is (no delete, no rewrite)."""
    source = make_source()
    _use_fast_chunk_and_embed(monkeypatch)
    _fake_crawl_items(
        monkeypatch,
        [
            {
                "url": "https://example.com/nm",
                "markdown": "# Title\nSome content that is indexed the first time around.",
                "heading_path": "Title",
                "fetch_ok": True,
            }
        ],
    )
    outcome1 = store.sync_source(source, conn)
    assert outcome1.pages_fetched == 1

    with conn.cursor() as cur:
        cur.execute("SELECT content_hash FROM doc_pages WHERE url = %s", ("https://example.com/nm",))
        (hash_before,) = cur.fetchone()

    _fake_crawl_items(
        monkeypatch,
        [{"url": "https://example.com/nm", "not_modified": True, "fetch_ok": True}],
    )
    outcome2 = store.sync_source(source, conn)
    assert outcome2.pages_not_modified == 1
    assert outcome2.pages_fetched == 0
    assert outcome2.pages_removed == 0

    with conn.cursor() as cur:
        cur.execute("SELECT content_hash FROM doc_pages WHERE url = %s", ("https://example.com/nm",))
        (hash_after,) = cur.fetchone()
    assert hash_after == hash_before


def test_sync_source_llms_index_unchanged_sentinel_skips_purge_entirely(conn, second_conn, monkeypatch):
    """A crawl yielding only the `llms_index_unchanged` sentinel must result
    in zero deletes (pages_removed == 0) even though doc_pages has rows for
    this source that were never in seen_urls this run."""
    source = make_source()
    existing_urls = [f"https://example.com/page-{i}" for i in range(5)]
    _seed_pages(conn, monkeypatch, source, existing_urls)

    _fake_crawl_items(
        monkeypatch,
        [
            {
                "kind": "llms_index_unchanged",
                "url": "https://example.com/llms-full.txt",
                "not_modified": True,
                "fetch_ok": True,
            }
        ],
    )

    outcome = store.sync_source(source, conn)

    assert outcome.pages_removed == 0
    assert outcome.status == "ok"
    assert _existing_urls(second_conn, source.name) == set(existing_urls)


def test_recovery_crash_resume_incremental(conn, second_conn, monkeypatch):
    """(B6) Crash-resume: sync dies mid-source after N pages; fresh sync ends with
    complete corpus and skips the first run's N pages by hash."""
    source = make_source()

    def crawl_run1(source, client=None):
        yield {"url": "https://example.com/p1", "html": PAGE_MD}
        yield {"url": "https://example.com/p2", "html": PAGE_MD}
        raise RuntimeError("connection lost after 2 pages")

    def fake_extract(url, html):
        from app.extract import ExtractionResult
        return ExtractionResult(url=url, markdown=html, status="ok")

    monkeypatch.setattr(store.crawler, "crawl", crawl_run1)
    monkeypatch.setattr(store.extract, "extract", fake_extract)

    outcome1 = store.sync_source(source, conn)
    assert outcome1.status == "partial"
    assert outcome1.pages_fetched == 2
    assert _existing_urls(second_conn, source.name) == {"https://example.com/p1", "https://example.com/p2"}

    def crawl_run2(source, client=None):
        yield {"url": "https://example.com/p1", "html": PAGE_MD}
        yield {"url": "https://example.com/p2", "html": PAGE_MD}
        yield {"url": "https://example.com/p3", "html": PAGE_MD}
        yield {"url": "https://example.com/p4", "html": PAGE_MD}

    monkeypatch.setattr(store.crawler, "crawl", crawl_run2)
    outcome2 = store.sync_source(source, conn)
    assert outcome2.pages_skipped == 2  # p1 and p2 skipped by hash!
    assert outcome2.pages_fetched == 2  # p3 and p4 indexed!
    assert outcome2.status == "ok"
    assert _existing_urls(second_conn, source.name) == {
        "https://example.com/p1",
        "https://example.com/p2",
        "https://example.com/p3",
        "https://example.com/p4",
    }


