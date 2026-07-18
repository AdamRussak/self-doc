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
import threading
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
# Defaults to ingestion/config/sources.yaml; override with the SOURCES_YAML
# env var (e.g. for tests pointing at a fixture file).
SOURCES_YAML = Path(
    os.environ.get("SOURCES_YAML", str(Path(__file__).parent.parent / "config" / "sources.yaml"))
)
try:
    _INITIAL_SOURCES: list[SourceConfig] = load_sources(SOURCES_YAML)
except ConfigError as e:
    print(f"FATAL: invalid sources.yaml ({SOURCES_YAML}): {e}", file=sys.stderr)
    raise SystemExit(1)

# --- Dynamic re-read with a last-known-good cache for test observability ----------------
# Startup (above) is fail-fast: invalid config at boot aborts the process
# before uvicorn binds. Runtime re-reads (below) must fail *soft*: a typo
# introduced in sources.yaml while the service is running must never crash
# it or empty out its source list mid-flight — callers get a ConfigError,
# which request handlers turn into an HTTP 400, and the process keeps
# running. `_last_good_sources` records the last successfully-loaded config
# for tests to assert against; it is not read on any production request path.
_sources_cache_lock = threading.Lock()
_last_good_sources: list[SourceConfig] = _INITIAL_SOURCES


def get_sources() -> list[SourceConfig]:
    """Re-read and validate SOURCES_YAML from disk on every call.

    On success, updates `_last_good_sources` and returns the fresh list.
    On `ConfigError`, logs a structured event and re-raises WITHOUT
    touching the cache — the previous last-known-good list is left
    untouched. `_last_good_sources` is a test-observable record only: no
    production call path reads it back as a fallback. The fail-soft
    behavior actually observed (service stays up, caller gets a 400)
    comes from the try/except around this call in the `/sync` handler,
    not from this cache.
    """
    global _last_good_sources
    try:
        sources = load_sources(SOURCES_YAML)
    except ConfigError as e:
        logger.error("sources_yaml_reload_failed", path=str(SOURCES_YAML), error=str(e))
        raise
    with _sources_cache_lock:
        _last_good_sources = sources
    return sources


def get_sources_by_name() -> dict[str, SourceConfig]:
    """Same contract as `get_sources()`, keyed by source name."""
    return {s.name: s for s in get_sources()}


def _get_last_good_sources_by_name() -> dict[str, SourceConfig]:
    """The last successfully-loaded config, without attempting a re-read.

    Not called from any request path — this is a test seam used to assert
    that the fail-soft cache survives a failed reload without triggering
    another `load_sources()` call (which would just re-raise the same
    `ConfigError`). Kept private (`_`-prefixed) since it has no production
    call site."""
    with _sources_cache_lock:
        return {s.name: s for s in _last_good_sources}


app = FastAPI(title="self-docs ingestion")

_sync_lock = asyncio.Lock()
_state: dict = {
    "running": False,
    "current": None,
    "results": {},  # source name -> summary dict
}

# --- Prometheus metrics ------------------------------------------------------------------
PAGES_FETCHED = Counter(
    "pages_fetched_total", "Pages fetched and (re)indexed (new or changed)", ["source"]
)
PAGES_SKIPPED = Counter(
    "pages_skipped_unchanged_total", "Pages skipped because their content hash is unchanged", ["source"]
)
PAGES_SOFT_FAILED = Counter(
    "pages_soft_failed_total", "Pages soft-failed due to expected site quirks (404/503 fetch or stub content)", ["source"]
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


def _run_sync_blocking(names: list[str], sources_by_name: dict[str, SourceConfig]) -> None:
    """Synchronous sync worker — runs in a worker thread via `asyncio.to_thread`
    so the event loop stays responsive for /status, /health, /metrics.

    `sources_by_name` is resolved ONCE by the caller (the /sync endpoint) and
    passed down here so a sources.yaml edit landing mid-sync cannot change
    the set of sources (or their config) being iterated partway through."""
    orig_crawl = store.crawler.crawl

    def tracking_crawl(src, *args, **kwargs):
        for page in orig_crawl(src, *args, **kwargs):
            if _state.get("current") and _state["current"].get("source") == src.name:
                _state["current"]["pages_processed"] += 1
            yield page

    store.crawler.crawl = tracking_crawl
    try:
        for i, name in enumerate(names):
            source = sources_by_name[name]
            log = logger.bind(source=name)
            start = time.monotonic()
            _state["current"] = {
                "source": name,
                "pages_processed": 0,
                "start_time": start,
                "position": f"{i + 1} of {len(names)}",
            }
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
                    # `conn` may be the reason we're here (e.g. a dropped
                    # connection), so `sync_source` never reached its own
                    # `_update_source_status` call — without this, doc_sources
                    # would show last_status=NULL, indistinguishable from "never
                    # ran". Best-effort on a fresh connection; never raises.
                    store.mark_source_failed(name)
                finally:
                    conn.close()
            duration = time.monotonic() - start

            PAGES_FETCHED.labels(source=name).inc(outcome.pages_fetched)
            PAGES_SKIPPED.labels(source=name).inc(outcome.pages_skipped)
            PAGES_SOFT_FAILED.labels(source=name).inc(outcome.pages_soft_failed)
            CHUNKS_INDEXED.labels(source=name).inc(outcome.chunks_indexed)
            SYNC_DURATION.labels(source=name).observe(duration)
            if outcome.status == "ok":
                SYNC_LAST_SUCCESS.labels(source=name).set(time.time())

            _state["results"][name] = {
                "pages_fetched": outcome.pages_fetched,
                "pages_skipped": outcome.pages_skipped,
                "pages_failed": outcome.pages_failed,
                "pages_soft_failed": outcome.pages_soft_failed,
                "pages_removed": outcome.pages_removed,
                "chunks_indexed": outcome.chunks_indexed,
                "last_status": outcome.status,
                "last_synced": time.time(),
                "error": outcome.error,
            }
    finally:
        store.crawler.crawl = orig_crawl


async def _sync_task(names: list[str], sources_by_name: dict[str, SourceConfig]) -> None:
    try:
        await asyncio.to_thread(_run_sync_blocking, names, sources_by_name)
    finally:
        _state["running"] = False
        _state["current"] = None
        if _sync_lock.locked():
            _sync_lock.release()


@app.post("/sync")
async def sync(req: SyncRequest | None = None, authorization: str | None = Header(default=None)):
    _check_auth(authorization)

    if _sync_lock.locked():
        raise HTTPException(status_code=409, detail="sync already running")

    # Resolve sources.yaml ONCE for this request: a re-read here fails soft
    # (this try/except turns a ConfigError into a 400 and the service keeps
    # running) rather than crashing — unlike the fail-fast startup load above.
    try:
        sources_by_name = get_sources_by_name()
    except ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))

    names = req.sources if (req and req.sources) else list(sources_by_name.keys())
    unknown = [n for n in names if n not in sources_by_name]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown source(s): {unknown}")

    await _sync_lock.acquire()
    _state["running"] = True
    _state["current"] = {
        "source": names[0] if names else "",
        "pages_processed": 0,
        "start_time": time.monotonic(),
        "position": f"1 of {len(names)}" if names else "0 of 0",
    }
    logger.info("sync_started", sources=names)
    asyncio.create_task(_sync_task(names, sources_by_name))
    return {"status": "started", "sources": names}


@app.get("/status")
async def status():
    res = {"running": _state["running"]}
    if _state["running"] and _state.get("current"):
        cur = dict(_state["current"])
        start_time = cur.pop("start_time", time.monotonic())
        cur["elapsed_s"] = int(time.monotonic() - start_time)
        res["current"] = cur
    res.update(_state["results"])
    return res


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8080)
