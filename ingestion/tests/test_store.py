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


@pytest.fixture()
def conn():
    c = store.get_connection()
    with c.cursor() as cur:
        cur.execute("DELETE FROM doc_sources")
    c.commit()
    yield c
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

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM doc_chunks")
        (chunk_count,) = cur.fetchone()
    assert chunk_count == outcome1.chunks_indexed

    # Second sync of the *same* content must skip the unchanged page entirely.
    outcome2 = store.sync_source(source, conn)
    assert outcome2.status == "ok"
    assert outcome2.pages_fetched == 0
    assert outcome2.pages_skipped == 1
    assert outcome2.chunks_indexed == 0

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM doc_chunks")
        (chunk_count_after,) = cur.fetchone()
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


def test_pages_removed_upstream_are_deleted(conn, monkeypatch):
    source = make_source()
    _fake_crawl_extract(
        monkeypatch,
        {"https://example.com/a": PAGE_MD, "https://example.com/b": PAGE_MD.replace("Intro", "Intro B")},
    )
    store.sync_source(source, conn)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM doc_pages")
        (count_before,) = cur.fetchone()
    assert count_before == 2

    # Next crawl only returns page a — page b was removed upstream.
    _fake_crawl_extract(monkeypatch, {"https://example.com/a": PAGE_MD})
    outcome = store.sync_source(source, conn)
    assert outcome.pages_removed == 1

    with conn.cursor() as cur:
        cur.execute("SELECT url FROM doc_pages")
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


def test_source_status_partial_on_some_page_failures(conn, monkeypatch):
    source = make_source()

    def fake_crawl(source, client=None):
        return [
            {"url": "https://example.com/good", "html": PAGE_MD},
            {"url": "https://example.com/bad", "html": "x"},  # too short -> extract "skipped"
        ]

    from app.extract import ExtractionResult

    def fake_extract(url, html):
        if html == "x":
            return ExtractionResult(url=url, markdown=None, status="skipped", reason="too short")
        return ExtractionResult(url=url, markdown=html, status="ok")

    monkeypatch.setattr(store.crawler, "crawl", fake_crawl)
    monkeypatch.setattr(store.extract, "extract", fake_extract)

    outcome = store.sync_source(source, conn)
    assert outcome.status == "partial"
    assert outcome.pages_fetched == 1
    assert outcome.pages_failed == 1


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
