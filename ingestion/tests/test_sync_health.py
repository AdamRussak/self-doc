"""Regression tests pinning the 2026-07-26 sync-health incident: every one
of the 40 doc sources reported `last_status='ok'` — including 6 that hold
zero pages — and `/metrics` emitted zero application samples.

These tests deliberately assert the CORRECT (currently absent) behavior and
are marked `xfail(strict=True)`: they must fail against today's code and
will start passing (and need their xfail markers removed) once the
corresponding fix lands. Keeping them `strict=True` means an unexpected pass
also fails the suite, so nobody can quietly "fix" only three of the four
cases and have this file go green by accident.

(a)/(b)/(c) reuse the live-DB `conn`/`second_conn` harness and monkeypatch
patterns from `test_store.py` (crawler.crawl / extract.extract doubles) —
skipped automatically when no Postgres is reachable, same as that module.

(d) reuses `test_admin.py`'s no-DB pattern: `app.admin._bg_sync_all` is
exercised directly with `store.sync_source` monkeypatched to a canned
`SourceOutcome`, so no live database is required for that case.
"""

from __future__ import annotations

import os

import psycopg
import pytest
from app import admin, store
from app.config import SourceConfig
from app.sources_repo import SourceRecord
from app.store import SourceOutcome

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


_DB_AVAILABLE = _db_available()

# Same scoping convention as test_store.py: every source created by
# make_source() below uses one of these names, so cleanup never touches the
# real indexed corpus (fastapi, traefik, docker-compose, ...).
_TEST_SOURCE_NAMES = ("sync-health-test-src",)


def _purge_test_sources(c) -> None:
    c.rollback()
    with c.cursor() as cur:
        cur.execute("DELETE FROM doc_sources WHERE name = ANY(%s)", (list(_TEST_SOURCE_NAMES),))
    c.commit()


@pytest.fixture()
def conn():
    if not _DB_AVAILABLE:
        pytest.skip("no live Postgres reachable for test_sync_health.py integration tests")
    c = store.get_connection()
    try:
        _purge_test_sources(c)
        yield c
    finally:
        _purge_test_sources(c)
        c.close()


@pytest.fixture()
def second_conn():
    if not _DB_AVAILABLE:
        pytest.skip("no live Postgres reachable for test_sync_health.py integration tests")
    c = store.get_connection()
    try:
        yield c
    finally:
        c.close()


def make_source(name: str = "sync-health-test-src", max_pages: int = 500) -> SourceConfig:
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
    """Replace the real chunker/embedder with trivial fast fakes — needed
    for the 150/280-page scale tests here, same rationale as test_store.py."""
    from app.embedder import EMBEDDING_DIM

    def fake_chunk_markdown(url, markdown):
        return [{"heading_path": [], "chunk_index": 0, "content": markdown}]

    def fake_embed_chunks(chunks):
        for c in chunks:
            c["embedding"] = [0.0] * EMBEDDING_DIM
        return chunks

    monkeypatch.setattr(store.chunker, "chunk_markdown", fake_chunk_markdown)
    monkeypatch.setattr(store.embedder, "embed_chunks", fake_embed_chunks)


def _seed_pages(conn, monkeypatch, source: SourceConfig, urls: list[str]) -> None:
    _fake_crawl_extract(monkeypatch, {u: PAGE_MD for u in urls})
    outcome = store.sync_source(source, conn)
    assert outcome.status == "ok"
    assert outcome.pages_fetched == len(urls)


def test_all_pages_soft_failed_should_be_failed_not_ok(conn, monkeypatch):
    """A sync where every single page soft-fails (0 successes at all) must
    be reported 'failed' — not 'ok'. Today `sync_source` only looks at
    `pages_failed` (hard failures) to decide status, so an entirely
    soft-failed crawl (0 hard failures) falls into the `pages_failed == 0`
    branch and is reported 'ok', hiding a source that indexed nothing this
    run."""
    source = make_source()

    def fake_crawl(source, client=None):
        for i in range(40):
            yield {"url": f"https://example.com/broken-{i}", "html": None, "fetch_ok": False}

    monkeypatch.setattr(store.crawler, "crawl", fake_crawl)

    outcome = store.sync_source(source, conn)

    assert outcome.pages_soft_failed == 40
    assert outcome.pages_fetched == 0
    assert outcome.pages_skipped == 0
    assert outcome.pages_failed == 0
    assert outcome.status == "failed"


def test_partial_soft_failure_ratio_should_be_partial_not_ok(conn, monkeypatch):
    """Real traefik-shaped case: 280 pages attempted, 163 succeed and 117
    soft-fail (42% content loss). Today's status logic treats any nonzero
    `pages_soft_failed` as still "succeeded" for status purposes as long as
    `pages_failed == 0`, so this reports 'ok' — masking a source that lost
    almost half its content this run. It should report 'partial'."""
    source = make_source()

    total_pages = 280
    soft_failed_count = 117
    ok_count = total_pages - soft_failed_count

    def fake_crawl(source, client=None):
        for i in range(ok_count):
            yield {"url": f"https://example.com/ok-{i}", "html": PAGE_MD}
        for i in range(soft_failed_count):
            yield {"url": f"https://example.com/broken-{i}", "html": None, "fetch_ok": False}

    def fake_extract(url, html):
        from app.extract import ExtractionResult

        return ExtractionResult(url=url, markdown=html, status="ok")

    _use_fast_chunk_and_embed(monkeypatch)
    monkeypatch.setattr(store.crawler, "crawl", fake_crawl)
    monkeypatch.setattr(store.extract, "extract", fake_extract)

    outcome = store.sync_source(source, conn)

    assert outcome.pages_fetched == ok_count
    assert outcome.pages_soft_failed == soft_failed_count
    assert outcome.pages_failed == 0
    assert outcome.status == "partial"


def test_purge_guard_refused_should_be_partial_not_ok(conn, second_conn, monkeypatch):
    """Real microsoft-clarity-shaped case: 150 existing pages, a crawl that
    (via BFS-collapse) only reaches 1 of them. coverage = 1/150 = 0.0067
    (well under the 0.3 floor), delete ratio = 149/150 = 0.993 (well over
    the 0.5 threshold) -> the purge-ratio guard correctly REFUSES to delete
    the other 149 pages. But `sync_source`'s status computation doesn't
    know a purge was refused: the one page that WAS crawled fetched fine
    (`pages_failed == 0`), so it still reports 'ok' — hiding a run that
    would have wiped 99% of the source had the guard not intervened. It
    should report 'partial'."""
    source = make_source(max_pages=500)
    _use_fast_chunk_and_embed(monkeypatch)

    existing_urls = [f"https://example.com/page-{i}" for i in range(150)]
    _seed_pages(conn, monkeypatch, source, existing_urls)

    # BFS collapse: only one page is reached this run.
    _fake_crawl_extract(monkeypatch, {existing_urls[0]: PAGE_MD})

    outcome = store.sync_source(source, conn)

    # The guard did its job: nothing was actually deleted.
    assert outcome.pages_removed == 0
    with second_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM doc_pages p JOIN doc_sources s ON s.id = p.source_id WHERE s.name = %s",
            (source.name,),
        )
        (remaining,) = cur.fetchone()
    assert remaining == 150

    assert outcome.status == "partial"


def _make_record(**overrides) -> SourceRecord:
    defaults = dict(
        id=1,
        name="metrics-test-src",
        base_url="https://metrics.example.com/docs/",
        sitemap=None,
        include_prefixes=[],
        exclude_prefixes=[],
        max_pages=100,
        language="english",
        rate_limit_rps=1.0,
        llms_txt="auto",
        schedule_cron=None,
        enabled=True,
        status="active",
        proposed_by=None,
        created_at=None,
        last_synced=None,
        last_status=None,
    )
    defaults.update(overrides)
    return SourceRecord(**defaults)


def _sample_value(counter_or_family, source_name: str):
    """Find the sample for label `source=source_name` in a prometheus_client
    metric's `.collect()` output, across every sample name emitted for that
    metric (a Counter emits `<name>_total` plus `<name>_created`; a
    Histogram emits `_bucket`/`_sum`/`_count`). Returns None if no sample
    for that label combination exists at all -- distinct from a sample that
    exists with value 0.0."""
    for family in counter_or_family.collect():
        for sample in family.samples:
            if sample.labels.get("source") == source_name:
                return sample.value
    return None


@pytest.mark.xfail(
    strict=True,
    reason="T0: admin-driven syncs never touch the Prometheus counters in main.py; /metrics reports zero samples",
)
def test_admin_driven_sync_should_record_prometheus_metrics(monkeypatch):
    """A sync driven through the ADMIN path (`admin.py`'s `_bg_sync_all` ->
    `store.sync_source`) must move the same Prometheus counters `/metrics`
    exposes. Today the 7 metrics (`PAGES_FETCHED`, `PAGES_SKIPPED`,
    `PAGES_NOT_MODIFIED`, `PAGES_SOFT_FAILED`, `CHUNKS_INDEXED`,
    `SYNC_DURATION`, `SYNC_LAST_SUCCESS`) are incremented ONLY inside the
    `POST /sync` route handler in `main.py` -- which the admin manual-sync
    and admin full-sync paths never go through -- so every sample for a
    source synced exclusively via the admin UI stays entirely absent from
    `/metrics`, even though real pages were fetched and chunks were
    written."""
    from app import main as main_module

    record = _make_record()
    canned_outcome = SourceOutcome(
        name=record.name,
        pages_fetched=7,
        pages_skipped=2,
        pages_not_modified=1,
        pages_soft_failed=0,
        pages_failed=0,
        pages_removed=0,
        chunks_indexed=13,
        status="ok",
    )

    def fake_sync_source(cfg, conn, progress_cb=None):
        return canned_outcome

    monkeypatch.setattr(store, "sync_source", fake_sync_source)

    admin._bg_sync_all([record], conn_factory=lambda: object())

    assert _sample_value(main_module.PAGES_FETCHED, record.name) == 7
    assert _sample_value(main_module.PAGES_SKIPPED, record.name) == 2
    assert _sample_value(main_module.PAGES_NOT_MODIFIED, record.name) == 1
    assert _sample_value(main_module.CHUNKS_INDEXED, record.name) == 13
    assert _sample_value(main_module.SYNC_DURATION, record.name) is not None
    assert _sample_value(main_module.SYNC_LAST_SUCCESS, record.name) is not None
