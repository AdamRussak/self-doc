"""Tests for the FastAPI service wrapper (app/main.py).

`app.main` reads `SYNC_TOKEN` (required) and connects to the database to
load `doc_sources` at *import* time (fail-fast), so each test that needs a
fresh import does so in a subprocess (for the startup-failure cases) or via
a one-time module-scoped import with `store.get_connection` /
`sources_repo.list_sources` patched to canned in-memory values (for the
request-handling cases) — the ingestion test container's Postgres port is
NOT published to the host, so pytest cannot open a real `psycopg` connection
to it; see `sources_repo.py`'s verification note for the same constraint on
that module.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


def _ingestion_root():
    from pathlib import Path

    return Path(__file__).parent.parent


def _make_record(
    id: int,
    name: str,
    *,
    status: str = "active",
    base_url: str | None = None,
    proposed_by: str | None = None,
):
    """Build a `SourceRecord` with plausible defaults for tests that don't
    care about most fields — only `id`/`name`/`status` typically matter."""
    from app.sources_repo import SourceRecord

    return SourceRecord(
        id=id,
        name=name,
        base_url=base_url or f"https://{name}.example.com/",
        sitemap=None,
        include_prefixes=[],
        exclude_prefixes=[],
        max_pages=10,
        language="english",
        rate_limit_rps=1.0,
        schedule_cron=None,
        enabled=True,
        status=status,
        proposed_by=proposed_by,
        created_at=datetime.now(timezone.utc),
        last_synced=None,
        last_status=None,
    )


def test_missing_sync_token_refuses_to_start(tmp_path):
    """Importing app.main without SYNC_TOKEN set must exit non-zero before
    uvicorn ever binds a socket — this check happens before any database
    connection is attempted."""
    env = os.environ.copy()
    env.pop("SYNC_TOKEN", None)
    env["POSTGRES_HOST"] = "127.0.0.1"
    env["POSTGRES_PORT"] = "5433"
    env["POSTGRES_USER"] = "self_docs"
    env["POSTGRES_PASSWORD"] = "testpass123"
    env["POSTGRES_DB"] = "self_docs"

    script = "import app.main"
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(_ingestion_root()),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "SYNC_TOKEN" in proc.stderr


def test_startup_fails_fast_on_unreachable_db():
    """STARTUP fail-fast: an unreachable database at import time must exit
    non-zero with a FATAL message on stderr, before uvicorn ever binds a
    socket. `doc_sources` (not sources.yaml) is now the source of truth for
    crawl config, so the boot-time check is "can we reach the database",
    not "is sources.yaml well-formed"."""
    env = os.environ.copy()
    env["SYNC_TOKEN"] = "test-token-123"
    env["POSTGRES_HOST"] = "127.0.0.1"
    env["POSTGRES_PORT"] = "1"  # nothing listens here -> ECONNREFUSED, fast
    env["POSTGRES_USER"] = "self_docs"
    env["POSTGRES_PASSWORD"] = "testpass123"
    env["POSTGRES_DB"] = "self_docs"

    proc = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=str(_ingestion_root()),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 0
    assert "FATAL" in proc.stderr
    assert "database" in proc.stderr.lower()


@pytest.fixture(scope="module")
def app_module():
    # Module-scoped + imported (not reloaded) once: prometheus_client's default
    # registry raises on duplicate series registration, so app.main must only
    # be imported a single time per test process.
    #
    # `app.main` now connects to the database at import time (fail-fast
    # startup check) and on every `/sync` request (get_sources()). Since
    # pytest cannot reach the real database from the host, `store.get_connection`
    # and `sources_repo.list_sources` are patched to canned in-memory values
    # BEFORE `app.main` is imported — the same "patch the module attribute,
    # not a bound name" trick already used by every DB-touching test below,
    # applied at import time instead of function scope.
    os.environ["SYNC_TOKEN"] = "test-token-123"
    os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
    os.environ.setdefault("POSTGRES_PORT", "5433")
    os.environ.setdefault("POSTGRES_USER", "self_docs")
    os.environ.setdefault("POSTGRES_PASSWORD", "testpass123")
    os.environ.setdefault("POSTGRES_DB", "self_docs")

    import app.sources_repo as sources_repo
    import app.store as store

    class _FakeConn:
        def close(self):
            pass

    default_records = [
        _make_record(1, "fastapi"),
        _make_record(2, "traefik"),
    ]

    orig_get_connection = store.get_connection
    orig_list_sources = sources_repo.list_sources
    store.get_connection = lambda: _FakeConn()
    sources_repo.list_sources = lambda conn: list(default_records)

    import app.main as m

    yield m

    store.get_connection = orig_get_connection
    sources_repo.list_sources = orig_list_sources


def test_sync_requires_bearer_auth(app_module):
    client = TestClient(app_module.app)

    resp = client.post("/sync")
    assert resp.status_code == 401

    resp = client.post("/sync", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401

    resp = client.get("/status")
    assert resp.status_code == 200
    assert "running" in resp.json()


def test_sources_yaml_constant_still_resolves_for_opt_in_import(app_module):
    """`get_sources()`/`get_sources_by_name()` no longer read sources.yaml —
    `doc_sources` is authoritative (see module docstring). `SOURCES_YAML`
    survives only as the path used by the opt-in
    `IMPORT_SOURCES_YAML_ON_BOOT` migration path
    (`_maybe_import_sources_yaml_on_boot`); assert the constant still
    resolves to a real file so that opt-in path isn't quietly broken by a
    future refactor."""
    assert "SOURCES_YAML" not in os.environ
    assert app_module.SOURCES_YAML.exists()
    assert app_module.SOURCES_YAML.name == "sources.yaml"


def test_health_ok(app_module):
    client = TestClient(app_module.app)
    resp = client.get("/health")
    assert resp.status_code == 200


def test_metrics_exposes_expected_series(app_module):
    client = TestClient(app_module.app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    for series in (
        "pages_fetched_total",
        "pages_skipped_unchanged_total",
        "chunks_indexed_total",
        "sync_duration_seconds",
        "sync_last_success_timestamp",
    ):
        assert series in body


def test_sync_rejects_unknown_source(app_module):
    client = TestClient(app_module.app)
    resp = client.post(
        "/sync",
        headers={"Authorization": "Bearer test-token-123"},
        json={"sources": ["does-not-exist"]},
    )
    assert resp.status_code == 400


def test_sync_unknown_single_source_id_returns_404(app_module):
    client = TestClient(app_module.app)
    resp = client.post(
        "/sync",
        headers={"Authorization": "Bearer test-token-123"},
        json={"source": 999999},
    )
    assert resp.status_code == 404


def test_sync_unknown_single_source_name_returns_404(app_module):
    client = TestClient(app_module.app)
    resp = client.post(
        "/sync",
        headers={"Authorization": "Bearer test-token-123"},
        json={"source": "does-not-exist"},
    )
    assert resp.status_code == 404


def test_sync_single_source_by_name(app_module, monkeypatch):
    """Single-source sync by NAME (admin UI manual-sync button contract)."""
    monkeypatch.setattr(app_module, "_run_sync_blocking", lambda names, sources_by_name: None)
    with TestClient(app_module.app) as client:
        resp = client.post(
            "/sync",
            headers={"Authorization": "Bearer test-token-123"},
            json={"source": "fastapi"},
        )
        assert resp.status_code == 200
        assert resp.json()["sources"] == ["fastapi"]


def test_sync_single_source_by_id(app_module, monkeypatch):
    """Single-source sync by ID (admin UI manual-sync button contract) —
    "traefik" is id=2 in the default fixture set built by `app_module`."""
    monkeypatch.setattr(app_module, "_run_sync_blocking", lambda names, sources_by_name: None)
    with TestClient(app_module.app) as client:
        resp = client.post(
            "/sync",
            headers={"Authorization": "Bearer test-token-123"},
            json={"source": 2},
        )
        assert resp.status_code == 200
        assert resp.json()["sources"] == ["traefik"]


def test_sync_pending_source_refused_by_id(app_module, monkeypatch):
    """SECURITY GATE: a source proposed via MCP lands status='pending' and
    must be uncrawlable via /sync until a human approves it — enforced in
    the sync path itself (`_resolve_sync_targets`), not the caller. Refused
    with a distinct (403, not 400/404/409) error identifying the status."""
    import app.sources_repo as sources_repo

    pending = _make_record(42, "pending-src", status="pending", proposed_by="mcp")
    monkeypatch.setattr(sources_repo, "list_sources", lambda conn: [pending])

    with TestClient(app_module.app) as client:
        resp = client.post(
            "/sync",
            headers={"Authorization": "Bearer test-token-123"},
            json={"source": "pending-src"},
        )
        assert resp.status_code == 403
        assert "pending" in resp.json()["detail"]


def test_sync_pending_source_refused_by_name_in_list(app_module, monkeypatch):
    """The same security gate applies to the classic {"sources": [...]}
    request shape, not just the new single-`source` field — a pending name
    slipped into a list must be refused too."""
    import app.sources_repo as sources_repo

    rejected = _make_record(43, "rejected-src", status="rejected")
    monkeypatch.setattr(sources_repo, "list_sources", lambda conn: [rejected])

    with TestClient(app_module.app) as client:
        resp = client.post(
            "/sync",
            headers={"Authorization": "Bearer test-token-123"},
            json={"sources": ["rejected-src"]},
        )
        assert resp.status_code == 403
        assert "rejected-src" in resp.json()["detail"]
        assert "rejected" in resp.json()["detail"]


def test_sync_default_all_sources_excludes_pending(app_module, monkeypatch):
    """An unscoped "sync everything" call (no `source`/`sources` in the
    request body) must never sweep up a pending or rejected source."""
    import app.sources_repo as sources_repo

    records = [
        _make_record(1, "active-a", status="active"),
        _make_record(2, "pending-b", status="pending"),
        _make_record(3, "rejected-c", status="rejected"),
    ]
    monkeypatch.setattr(sources_repo, "list_sources", lambda conn: records)
    monkeypatch.setattr(app_module, "_run_sync_blocking", lambda names, sources_by_name: None)

    with TestClient(app_module.app) as client:
        resp = client.post("/sync", headers={"Authorization": "Bearer test-token-123"})
        assert resp.status_code == 200
        assert resp.json()["sources"] == ["active-a"]


def test_sync_source_crash_marks_status_failed_on_fresh_connection(app_module, monkeypatch):
    """If `store.sync_source` itself crashes (e.g. its connection died mid
    sync — the "connection is lost" incident this task fixes), the crash
    handler must still persist last_status='failed' via a *new* connection,
    since the one `sync_source` was using may be the reason it crashed."""
    import app.store as store

    calls = []

    class FakeConn:
        def close(self):
            pass

    monkeypatch.setattr(store, "get_connection", lambda: FakeConn())

    def crashing_sync_source(source, conn, **kwargs):
        raise RuntimeError("the connection is lost")

    monkeypatch.setattr(store, "sync_source", crashing_sync_source)
    monkeypatch.setattr(store, "mark_source_failed", lambda name: calls.append(name))

    sources_by_name = app_module.get_sources_by_name()
    name = next(iter(sources_by_name))
    app_module._run_sync_blocking([name], sources_by_name)

    assert calls == [name]
    assert app_module._state["results"][name]["last_status"] == "failed"


def test_sync_second_call_returns_409_while_running(app_module, monkeypatch):
    import asyncio
    import time

    # Make the sync worker slow so we can observe the "running" state.
    def slow_sync(names, sources_by_name):
        time.sleep(0.5)

    monkeypatch.setattr(app_module, "_run_sync_blocking", slow_sync)

    # Use the TestClient as a context manager so the underlying event loop
    # (and its portal) stays alive across both calls — otherwise the portal
    # tears down (and waits for the background `to_thread` task) between
    # calls, masking the lock-contention behavior we're testing.
    with TestClient(app_module.app) as client:
        resp1 = client.post(
            "/sync",
            headers={"Authorization": "Bearer test-token-123"},
            json={"sources": ["fastapi"]},
        )
        assert resp1.status_code == 200

        resp2 = client.post(
            "/sync",
            headers={"Authorization": "Bearer test-token-123"},
            json={"sources": ["fastapi"]},
        )
        assert resp2.status_code == 409


def test_status_reports_in_flight_sync_progress(app_module, monkeypatch):
    import time

    def slow_sync_blocking(names, sources_by_name):
        app_module._state["current"] = {
            "source": names[0],
            "pages_processed": 10,
            "start_time": time.monotonic() - 5.0,
            "position": f"1 of {len(names)}",
        }
        time.sleep(0.3)
        app_module._state["current"]["pages_processed"] = 25
        time.sleep(0.3)

    monkeypatch.setattr(app_module, "_run_sync_blocking", slow_sync_blocking)

    with TestClient(app_module.app) as client:
        resp_post = client.post(
            "/sync",
            headers={"Authorization": "Bearer test-token-123"},
            json={"sources": ["fastapi", "traefik"]},
        )
        assert resp_post.status_code == 200

        time.sleep(0.1)
        resp_status1 = client.get("/status")
        assert resp_status1.status_code == 200
        data1 = resp_status1.json()
        assert data1["running"] is True
        assert "current" in data1
        assert data1["current"]["source"] == "fastapi"
        assert data1["current"]["pages_processed"] == 10
        assert data1["current"]["position"] == "1 of 2"
        assert isinstance(data1["current"]["elapsed_s"], int)
        assert data1["current"]["elapsed_s"] >= 5

        time.sleep(0.3)
        resp_status2 = client.get("/status")
        assert resp_status2.status_code == 200
        data2 = resp_status2.json()
        assert data2["running"] is True
        assert data2["current"]["pages_processed"] == 25

        time.sleep(0.5)
        resp_status3 = client.get("/status")
        assert resp_status3.status_code == 200
        data3 = resp_status3.json()
        assert data3["running"] is False
        assert "current" not in data3 or data3.get("current") is None


def test_run_sync_blocking_updates_pages_processed(app_module, monkeypatch):
    """Verify that _run_sync_blocking wraps store.crawler.crawl and increments
    pages_processed dynamically as pages are yielded."""
    import app.store as store

    def fake_crawl(source, client=None):
        yield {"url": "https://example.com/p1", "html": "# page 1"}
        assert app_module._state["current"]["pages_processed"] == 1
        yield {"url": "https://example.com/p2", "html": "# page 2"}
        assert app_module._state["current"]["pages_processed"] == 2

    monkeypatch.setattr(store.crawler, "crawl", fake_crawl)
    monkeypatch.setattr(
        store,
        "sync_source",
        lambda source, conn: store.SourceOutcome(name=source.name, pages_fetched=2, status="ok"),
    )

    class FakeConn:
        def close(self):
            pass

    monkeypatch.setattr(store, "get_connection", lambda: FakeConn())

    sources_by_name = app_module.get_sources_by_name()
    name = next(iter(sources_by_name))
    app_module._run_sync_blocking([name], sources_by_name)


def test_configure_logging_suppresses_third_party_loggers():
    import logging
    from app.logging_config import configure_logging

    configure_logging()
    for name in ("httpx", "httpcore", "uvicorn.access"):
        assert logging.getLogger(name).level == logging.WARNING


def test_run_sync_blocking_records_pages_soft_failed(app_module, monkeypatch):
    from app import store

    class FakeConn:
        def close(self):
            pass

    monkeypatch.setattr(store, "get_connection", lambda: FakeConn())
    monkeypatch.setattr(
        store,
        "sync_source",
        lambda source, conn: store.SourceOutcome(
            name=source.name,
            pages_soft_failed=3,
            status="ok",
        ),
    )

    sources_by_name = app_module.get_sources_by_name()
    name = next(iter(sources_by_name))
    before_count = app_module.PAGES_SOFT_FAILED.labels(source=name)._value.get()
    app_module._run_sync_blocking([name], sources_by_name)
    after_count = app_module.PAGES_SOFT_FAILED.labels(source=name)._value.get()

    assert app_module._state["results"][name]["pages_soft_failed"] == 3
    assert after_count - before_count == 3


def test_get_sources_by_name_reflects_latest_db_read(app_module, monkeypatch):
    """A running service must pick up doc_sources edits without a restart:
    get_sources()/get_sources_by_name() re-read the database on every call
    rather than caching it once at import time."""
    import app.sources_repo as sources_repo

    monkeypatch.setattr(
        sources_repo, "list_sources", lambda conn: [_make_record(1, "reread-a")]
    )
    first = app_module.get_sources_by_name()
    assert set(first.keys()) == {"reread-a"}

    monkeypatch.setattr(
        sources_repo, "list_sources", lambda conn: [_make_record(2, "reread-b")]
    )
    second = app_module.get_sources_by_name()
    assert set(second.keys()) == {"reread-b"}


def test_sync_db_failure_midflight_fails_soft_and_keeps_serving(app_module, monkeypatch):
    """The highest-value safety test: a database failure while re-reading
    sources at request time (DB restart, network blip, ...) must return
    HTTP 503 on that /sync call — it must NOT crash the service or wipe out
    the previously-good source list (`_last_good_sources`). The very next
    request, once the database is reachable again, must succeed normally."""
    import time as time_mod

    import app.sources_repo as sources_repo

    good_record = _make_record(100, "soft-a")
    calls = {"n": 0}

    def flaky_list_sources(conn):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated DB outage")
        return [good_record]

    monkeypatch.setattr(sources_repo, "list_sources", flaky_list_sources)
    # Avoid any real crawl/store work — this test is only about the
    # get_sources() fail-soft/fail-fast distinction around /sync.
    monkeypatch.setattr(app_module, "_run_sync_blocking", lambda names, sources_by_name: None)

    def _wait_for_lock_release():
        for _ in range(50):
            if not app_module._sync_lock.locked():
                return
            time_mod.sleep(0.02)

    with TestClient(app_module.app) as client:
        resp1 = client.post(
            "/sync",
            headers={"Authorization": "Bearer test-token-123"},
            json={"source": "soft-a"},
        )
        assert resp1.status_code == 200
        assert resp1.json()["sources"] == ["soft-a"]
        _wait_for_lock_release()

        # The database becomes unreachable on this request's re-read.
        resp2 = client.post(
            "/sync",
            headers={"Authorization": "Bearer test-token-123"},
            json={"source": "soft-a"},
        )
        assert resp2.status_code == 503
        # The last-known-good config must be untouched by the failed re-read.
        assert set(app_module._get_last_good_sources_by_name().keys()) == {"soft-a"}
        _wait_for_lock_release()

        # The database is reachable again — the service must still be
        # serving normally, with no restart required.
        resp3 = client.post(
            "/sync",
            headers={"Authorization": "Bearer test-token-123"},
            json={"source": "soft-a"},
        )
        assert resp3.status_code == 200
        assert resp3.json()["sources"] == ["soft-a"]


# --- Task B5: unify the sync lock across /sync, admin manual sync, and the scheduler -----
#
# Before this task there were THREE independent, non-cooperating sync
# entrypoints: `POST /sync` (this module's own `asyncio.Lock`), the admin
# manual-sync route (`admin._manual_sync_lock`, a *different*
# `threading.Lock`), and the scheduler (`scheduler.is_sync_locked_fn`, an
# unwired seam that always reported "not locked"). Any two could run a sync
# concurrently against the same source, corrupting the page-purge
# accounting. The tests below prove all three pairwise combinations now
# mutually exclude through the ONE shared `main._sync_lock`.


def test_admin_router_included_and_still_auth_gated(app_module):
    """Acceptance criterion #2: admin routes are reachable once wired in
    (not a 404 — the router really is mounted), but inclusion must not
    bypass admin.py's own per-route auth: an unauthenticated request still
    gets a 401, not the page content."""
    with TestClient(app_module.app) as client:
        resp = client.get("/admin")
        assert resp.status_code == 401

        resp2 = client.get("/admin/login")
        assert resp2.status_code == 200
        assert "SYNC_TOKEN" in resp2.text or "token" in resp2.text.lower()


def test_lock_unification_main_sync_blocks_admin_manual_sync(app_module, monkeypatch):
    """Pair 1/3, direction A: a `/sync` in flight must make the admin
    manual-sync route see the SAME lock as busy (409), not run a second,
    concurrent sync_source call."""
    import time as time_mod

    import app.sources_repo as sources_repo

    admin = app_module.admin
    record = _make_record(50, "lockcheck-a")
    monkeypatch.setattr(sources_repo, "list_sources", lambda conn: [record])
    monkeypatch.setattr(admin.sources_repo, "get_source", lambda conn, source_id: record)

    def slow_run_sync_blocking(names, sources_by_name):
        time_mod.sleep(0.4)

    monkeypatch.setattr(app_module, "_run_sync_blocking", slow_run_sync_blocking)

    sync_calls = []
    monkeypatch.setattr(
        admin.store, "sync_source", lambda cfg, conn, **kwargs: sync_calls.append(cfg.name)
    )

    # base_url is https (not the default http://testserver): the admin
    # session cookie is Secure now, and httpx's cookie jar — correctly —
    # refuses to attach a Secure cookie to a plain-http request, so a
    # plain-http TestClient would silently drop the cookie after login and
    # the "authenticated" admin request below would 401 instead of 409.
    with TestClient(app_module.app, base_url="https://testserver") as client:
        resp = client.post(
            "/sync",
            headers={"Authorization": "Bearer test-token-123"},
            json={"source": "lockcheck-a"},
        )
        assert resp.status_code == 200

        login_resp = client.post(
            "/admin/login", data={"token": "test-token-123"}, follow_redirects=False
        )
        assert login_resp.status_code == 303
        csrf = admin._expected_csrf_token()

        resp2 = client.post(
            "/admin/sources/50/sync",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp2.status_code == 409
        assert sync_calls == []


def test_lock_unification_admin_manual_sync_blocks_main_sync(app_module, monkeypatch):
    """Pair 1/3, direction B: an admin manual sync in flight must make
    `POST /sync` see the SAME lock as busy (409)."""
    import threading
    import time as time_mod

    import app.sources_repo as sources_repo
    from app.store import SourceOutcome

    admin = app_module.admin
    record = _make_record(51, "lockcheck-b")
    monkeypatch.setattr(sources_repo, "list_sources", lambda conn: [record])
    monkeypatch.setattr(admin.sources_repo, "get_source", lambda conn, source_id: record)

    def slow_sync_source(cfg, conn, **kwargs):
        time_mod.sleep(0.4)
        return SourceOutcome(name=cfg.name, status="ok")

    monkeypatch.setattr(admin.store, "sync_source", slow_sync_source)

    # base_url is https (not the default http://testserver): see comment on
    # the Secure-cookie/httpx-jar interaction above.
    with TestClient(app_module.app, base_url="https://testserver") as client:
        login_resp = client.post(
            "/admin/login", data={"token": "test-token-123"}, follow_redirects=False
        )
        assert login_resp.status_code == 303
        csrf = admin._expected_csrf_token()

        def _fire_admin_sync():
            client.post(
                "/admin/sources/51/sync",
                data={"csrf_token": csrf},
                follow_redirects=False,
            )

        t = threading.Thread(target=_fire_admin_sync)
        t.start()
        try:
            time_mod.sleep(0.1)
            resp = client.post(
                "/sync",
                headers={"Authorization": "Bearer test-token-123"},
                json={"source": "lockcheck-b"},
            )
            assert resp.status_code == 409
        finally:
            t.join(timeout=2)


def test_lock_unification_main_sync_blocks_scheduler_trigger(app_module, monkeypatch):
    """Pair 2/3, direction A: a `/sync` in flight must make the scheduler's
    `trigger_sync_fn` (wired to `main._scheduler_trigger_sync`) skip as a
    clean no-op rather than running a second, concurrent sync."""
    import asyncio as asyncio_mod
    import time as time_mod

    import app.sources_repo as sources_repo

    record = _make_record(52, "lockcheck-c")
    monkeypatch.setattr(sources_repo, "list_sources", lambda conn: [record])

    calls = []

    def slow_run_sync_blocking(names, sources_by_name):
        time_mod.sleep(0.4)
        calls.append(names)

    monkeypatch.setattr(app_module, "_run_sync_blocking", slow_run_sync_blocking)

    with TestClient(app_module.app) as client:
        resp = client.post(
            "/sync",
            headers={"Authorization": "Bearer test-token-123"},
            json={"source": "lockcheck-c"},
        )
        assert resp.status_code == 200

        # The scheduler's trigger must see the lock as held and skip
        # immediately (no blocking wait, no second sync attempt) — this
        # asyncio.run() call must return near-instantly, not after 0.4s.
        start = time_mod.monotonic()
        asyncio_mod.run(app_module._scheduler_trigger_sync(["lockcheck-c"]))
        elapsed = time_mod.monotonic() - start
        assert elapsed < 0.3

        time_mod.sleep(0.6)
        assert calls == [["lockcheck-c"]]


def test_lock_unification_scheduler_blocks_main_sync(app_module, monkeypatch):
    """Pair 2/3, direction B: a scheduler-triggered sync in flight must make
    `POST /sync` see the SAME lock as busy (409)."""
    import threading
    import time as time_mod

    import app.sources_repo as sources_repo

    record = _make_record(53, "lockcheck-d")
    monkeypatch.setattr(sources_repo, "list_sources", lambda conn: [record])

    def slow_run_sync_blocking(names, sources_by_name):
        time_mod.sleep(0.4)

    monkeypatch.setattr(app_module, "_run_sync_blocking", slow_run_sync_blocking)

    def _fire_scheduler_trigger():
        import asyncio as asyncio_mod

        asyncio_mod.run(app_module._scheduler_trigger_sync(["lockcheck-d"]))

    t = threading.Thread(target=_fire_scheduler_trigger)
    t.start()
    try:
        time_mod.sleep(0.1)
        with TestClient(app_module.app) as client:
            resp = client.post(
                "/sync",
                headers={"Authorization": "Bearer test-token-123"},
                json={"source": "lockcheck-d"},
            )
            assert resp.status_code == 409
    finally:
        t.join(timeout=2)


def test_lock_unification_admin_manual_sync_blocks_scheduler_trigger(app_module, monkeypatch):
    """Pair 3/3, direction A: an admin manual sync in flight must make the
    scheduler's trigger skip as a clean no-op."""
    import asyncio as asyncio_mod
    import time as time_mod

    import app.sources_repo as sources_repo

    admin = app_module.admin
    record = _make_record(54, "lockcheck-e")
    monkeypatch.setattr(sources_repo, "list_sources", lambda conn: [record])
    monkeypatch.setattr(admin.sources_repo, "get_source", lambda conn, source_id: record)

    calls = []

    def slow_sync_source(cfg, conn, **kwargs):
        time_mod.sleep(0.4)
        from app.store import SourceOutcome

        calls.append(cfg.name)
        return SourceOutcome(name=cfg.name, status="ok")

    monkeypatch.setattr(admin.store, "sync_source", slow_sync_source)

    # base_url is https (not the default http://testserver): see comment on
    # the Secure-cookie/httpx-jar interaction above.
    with TestClient(app_module.app, base_url="https://testserver") as client:
        login_resp = client.post(
            "/admin/login", data={"token": "test-token-123"}, follow_redirects=False
        )
        assert login_resp.status_code == 303
        csrf = admin._expected_csrf_token()

        import threading

        def _fire_admin_sync():
            client.post(
                "/admin/sources/54/sync",
                data={"csrf_token": csrf},
                follow_redirects=False,
            )

        t = threading.Thread(target=_fire_admin_sync)
        t.start()
        try:
            time_mod.sleep(0.1)
            start = time_mod.monotonic()
            asyncio_mod.run(app_module._scheduler_trigger_sync(["lockcheck-e"]))
            elapsed = time_mod.monotonic() - start
            assert elapsed < 0.3
        finally:
            t.join(timeout=2)

    assert calls == ["lockcheck-e"]


def test_lock_unification_scheduler_blocks_admin_manual_sync(app_module, monkeypatch):
    """Pair 3/3, direction B: a scheduler-triggered sync in flight must make
    the admin manual-sync route see the SAME lock as busy (409)."""
    import threading
    import time as time_mod

    import app.sources_repo as sources_repo

    admin = app_module.admin
    record = _make_record(55, "lockcheck-f")
    monkeypatch.setattr(sources_repo, "list_sources", lambda conn: [record])
    monkeypatch.setattr(admin.sources_repo, "get_source", lambda conn, source_id: record)

    def slow_run_sync_blocking(names, sources_by_name):
        time_mod.sleep(0.4)

    monkeypatch.setattr(app_module, "_run_sync_blocking", slow_run_sync_blocking)

    sync_calls = []
    monkeypatch.setattr(
        admin.store, "sync_source", lambda cfg, conn, **kwargs: sync_calls.append(cfg.name)
    )

    def _fire_scheduler_trigger():
        import asyncio as asyncio_mod

        asyncio_mod.run(app_module._scheduler_trigger_sync(["lockcheck-f"]))

    t = threading.Thread(target=_fire_scheduler_trigger)
    t.start()
    try:
        time_mod.sleep(0.1)
        # base_url is https (not the default http://testserver): see comment
        # on the Secure-cookie/httpx-jar interaction above.
        with TestClient(app_module.app, base_url="https://testserver") as client:
            login_resp = client.post(
                "/admin/login", data={"token": "test-token-123"}, follow_redirects=False
            )
            assert login_resp.status_code == 303
            csrf = admin._expected_csrf_token()

            resp = client.post(
                "/admin/sources/55/sync",
                data={"csrf_token": csrf},
                follow_redirects=False,
            )
            assert resp.status_code == 409
            assert sync_calls == []
    finally:
        t.join(timeout=2)


# --- Scheduler lifespan wiring -------------------------------------------------------------


def test_scheduler_parse_enabled_flag_truthy_and_falsy_values():
    """`SCHEDULER_ENABLED` string parsing, table-tested directly (no
    subprocess reimport needed — this has no fail-fast side effect at import
    time, unlike SYNC_TOKEN)."""
    from app.main import _parse_scheduler_enabled

    for truthy in ("1", "true", "True", "TRUE", "yes", "Yes"):
        assert _parse_scheduler_enabled(truthy) is True
    for falsy in (None, "", "0", "false", "False", "no", "off", "  "):
        assert _parse_scheduler_enabled(falsy) is False


def test_scheduler_disabled_by_default(app_module):
    """Priority 3: SCHEDULER_ENABLED defaults OFF. The shared `app_module`
    fixture imports app.main with no SCHEDULER_ENABLED env var set."""
    assert "SCHEDULER_ENABLED" not in os.environ
    assert app_module.SCHEDULER_ENABLED is False


def test_lifespan_noop_when_scheduler_disabled(app_module, monkeypatch):
    """Acceptance criterion #4: SCHEDULER_ENABLED=false must fully disable
    the scheduler — no background task started, no DB polling at all."""
    import asyncio as asyncio_mod

    monkeypatch.setattr(app_module, "SCHEDULER_ENABLED", False)
    calls = []
    monkeypatch.setattr(
        app_module.scheduler, "fetch_candidates_fn", lambda: calls.append("polled") or []
    )

    async def _run():
        async with app_module._lifespan(app_module.app):
            assert app_module._scheduler_task is None
            await asyncio_mod.sleep(0.05)

    asyncio_mod.run(_run())
    assert calls == []


def test_lifespan_starts_and_stops_scheduler_promptly_when_enabled(app_module, monkeypatch):
    """Acceptance criterion #3: with the scheduler enabled, the lifespan
    context starts a background task on entry and — critically — shuts it
    down PROMPTLY on exit (asserting a real wall-clock bound), not by
    waiting out the full `poll_interval_seconds`. A scheduler that blocks
    shutdown turns every deploy into a SIGKILL wait."""
    import asyncio as asyncio_mod
    import time as time_mod

    monkeypatch.setattr(app_module, "SCHEDULER_ENABLED", True)
    # Deliberately NOT shrinking poll_interval_seconds below its real
    # default: the point of this test is that shutdown doesn't wait for it.
    monkeypatch.setattr(app_module.scheduler, "fetch_candidates_fn", lambda: [])

    async def _run():
        async with app_module._lifespan(app_module.app):
            assert app_module._scheduler_task is not None
            assert not app_module._scheduler_task.done()
        assert app_module._scheduler_task is None
        assert app_module._scheduler_stop_event is None

    start = time_mod.monotonic()
    asyncio_mod.run(_run())
    elapsed = time_mod.monotonic() - start
    assert elapsed < 2.0
