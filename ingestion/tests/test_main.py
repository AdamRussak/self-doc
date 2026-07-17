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
