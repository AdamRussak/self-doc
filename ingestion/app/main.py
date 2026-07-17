"""FastAPI service wrapper for the ingestion engine.

Endpoints (per IMPLEMENTATION_PLAN.md §2 and the T4 task description):

  - POST /sync    {"sources": [names]} optional -> runs a sync as a
                  background task guarded by an asyncio lock; a second call
                  while one is running returns 409. Requires
                  `Authorization: Bearer $SYNC_TOKEN`.
  - GET  /status  per-source last-run summary + top-level {"running": bool}.
  - GET  /health  200 liveness probe.
  - GET  /metrics prometheus-client exposition.

`SYNC_TOKEN` is REQUIRED: the process refuses to start (exits non-zero) if
it is unset, at import time — before uvicorn ever binds a socket.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel

from . import store
from .config import ConfigError, SourceConfig, load_sources
from .logging_config import get_logger

logger = get_logger(component="main")

# --- Fail fast: SYNC_TOKEN is mandatory -------------------------------------------------
SYNC_TOKEN = os.environ.get("SYNC_TOKEN")
if not SYNC_TOKEN:
    print(
        "FATAL: SYNC_TOKEN environment variable is required but not set. Refusing to start.",
        file=sys.stderr,
    )
    raise SystemExit(1)

# --- Load + validate sources.yaml at startup --------------------------------------------
SOURCES_YAML = Path(os.environ.get("SOURCES_YAML", str(Path(__file__).parent / "sources.yaml")))
try:
    ALL_SOURCES: list[SourceConfig] = load_sources(SOURCES_YAML)
except ConfigError as e:
    print(f"FATAL: invalid sources.yaml ({SOURCES_YAML}): {e}", file=sys.stderr)
    raise SystemExit(1)

SOURCES_BY_NAME: dict[str, SourceConfig] = {s.name: s for s in ALL_SOURCES}

app = FastAPI(title="self-docs ingestion")

_sync_lock = asyncio.Lock()
_state: dict = {
    "running": False,
    "results": {},  # source name -> summary dict
}

# --- Prometheus metrics ------------------------------------------------------------------
PAGES_FETCHED = Counter(
    "pages_fetched_total", "Pages fetched and (re)indexed (new or changed)", ["source"]
)
PAGES_SKIPPED = Counter(
    "pages_skipped_unchanged_total", "Pages skipped because their content hash is unchanged", ["source"]
)
CHUNKS_INDEXED = Counter("chunks_indexed_total", "Chunks written to doc_chunks", ["source"])
SYNC_DURATION = Histogram("sync_duration_seconds", "Duration of a full sync run for one source", ["source"])
SYNC_LAST_SUCCESS = Gauge(
    "sync_last_success_timestamp", "Unix timestamp of the last successful (status=ok) sync", ["source"]
)


class SyncRequest(BaseModel):
    sources: list[str] | None = None


def _check_auth(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="unauthorized")
    token = authorization[len("Bearer "):]
    if not hmac.compare_digest(token, SYNC_TOKEN):
        raise HTTPException(status_code=401, detail="unauthorized")


def _run_sync_blocking(names: list[str]) -> None:
    """Synchronous sync worker — runs in a worker thread via `asyncio.to_thread`
    so the event loop stays responsive for /status, /health, /metrics."""
    for name in names:
        source = SOURCES_BY_NAME[name]
        log = logger.bind(source=name)
        start = time.monotonic()
        try:
            conn = store.get_connection()
        except Exception as e:  # noqa: BLE001
            log.error("sync_db_connect_failed", error=str(e))
            outcome = store.SourceOutcome(name=name, status="failed", error=str(e))
        else:
            try:
                outcome = store.sync_source(source, conn)
            except Exception as e:  # noqa: BLE001 - source-level safety net
                log.error("sync_source_crashed", error=str(e))
                outcome = store.SourceOutcome(name=name, status="failed", error=str(e))
            finally:
                conn.close()
        duration = time.monotonic() - start

        PAGES_FETCHED.labels(source=name).inc(outcome.pages_fetched)
        PAGES_SKIPPED.labels(source=name).inc(outcome.pages_skipped)
        CHUNKS_INDEXED.labels(source=name).inc(outcome.chunks_indexed)
        SYNC_DURATION.labels(source=name).observe(duration)
        if outcome.status == "ok":
            SYNC_LAST_SUCCESS.labels(source=name).set(time.time())

        _state["results"][name] = {
            "pages_fetched": outcome.pages_fetched,
            "pages_skipped": outcome.pages_skipped,
            "pages_failed": outcome.pages_failed,
            "pages_removed": outcome.pages_removed,
            "chunks_indexed": outcome.chunks_indexed,
            "last_status": outcome.status,
            "last_synced": time.time(),
            "error": outcome.error,
        }


async def _sync_task(names: list[str]) -> None:
    try:
        await asyncio.to_thread(_run_sync_blocking, names)
    finally:
        _state["running"] = False
        if _sync_lock.locked():
            _sync_lock.release()


@app.post("/sync")
async def sync(req: SyncRequest | None = None, authorization: str | None = Header(default=None)):
    _check_auth(authorization)

    if _sync_lock.locked():
        raise HTTPException(status_code=409, detail="sync already running")

    names = req.sources if (req and req.sources) else list(SOURCES_BY_NAME.keys())
    unknown = [n for n in names if n not in SOURCES_BY_NAME]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown source(s): {unknown}")

    await _sync_lock.acquire()
    _state["running"] = True
    logger.info("sync_started", sources=names)
    asyncio.create_task(_sync_task(names))
    return {"status": "started", "sources": names}


@app.get("/status")
async def status():
    return {"running": _state["running"], **_state["results"]}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8080)
