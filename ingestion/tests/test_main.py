"""Tests for the FastAPI service wrapper (app/main.py).

`app.main` reads `SYNC_TOKEN` (required) and validates `sources.yaml` at
*import* time, so each test that needs a fresh import does so in a
subprocess (for the startup-failure case) or via `importlib.reload` with the
right env vars pre-set (for the request-handling cases).
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient


def test_missing_sync_token_refuses_to_start(tmp_path):
    """Importing app.main without SYNC_TOKEN set must exit non-zero before
    uvicorn ever binds a socket."""
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


def _ingestion_root():
    from pathlib import Path

    return Path(__file__).parent.parent


@pytest.fixture(scope="module")
def app_module():
    # Module-scoped + imported (not reloaded) once: prometheus_client's default
    # registry raises on duplicate series registration, so app.main must only
    # be imported a single time per test process.
    os.environ["SYNC_TOKEN"] = "test-token-123"
    os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
    os.environ.setdefault("POSTGRES_PORT", "5433")
    os.environ.setdefault("POSTGRES_USER", "self_docs")
    os.environ.setdefault("POSTGRES_PASSWORD", "testpass123")
    os.environ.setdefault("POSTGRES_DB", "self_docs")

    import app.main as m

    yield m


def test_sync_requires_bearer_auth(app_module):
    client = TestClient(app_module.app)

    resp = client.post("/sync")
    assert resp.status_code == 401

    resp = client.post("/sync", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401

    resp = client.get("/status")
    assert resp.status_code == 200
    assert "running" in resp.json()


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

    def crashing_sync_source(source, conn):
        raise RuntimeError("the connection is lost")

    monkeypatch.setattr(store, "sync_source", crashing_sync_source)
    monkeypatch.setattr(store, "mark_source_failed", lambda name: calls.append(name))

    name = next(iter(app_module.SOURCES_BY_NAME))
    app_module._run_sync_blocking([name])

    assert calls == [name]
    assert app_module._state["results"][name]["last_status"] == "failed"


def test_sync_second_call_returns_409_while_running(app_module, monkeypatch):
    import asyncio
    import time

    # Make the sync worker slow so we can observe the "running" state.
    def slow_sync(names):
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

    def slow_sync_blocking(names):
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

    name = next(iter(app_module.SOURCES_BY_NAME))
    app_module._run_sync_blocking([name])


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

    name = next(iter(app_module.SOURCES_BY_NAME))
    before_count = app_module.PAGES_SOFT_FAILED.labels(source=name)._value.get()
    app_module._run_sync_blocking([name])
    after_count = app_module.PAGES_SOFT_FAILED.labels(source=name)._value.get()

    assert app_module._state["results"][name]["pages_soft_failed"] == 3
    assert after_count - before_count == 3


