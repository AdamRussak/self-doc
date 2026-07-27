"""FastAPI service wrapper for the ingestion engine.

Endpoints (per IMPLEMENTATION_PLAN.md §2 and the T4/B1 task descriptions):

  - POST /sync    {"sources": [names]} or {"source": id|name} optional -> runs
                  a sync as a background task guarded by an asyncio lock; a
                  second call while one is running returns 409. `source`
                  (single id or name, for the admin UI's manual-sync button)
                  and `sources` (a list of names, today's contract) are
                  mutually exclusive — `source` wins if both are given. With
                  neither, every ACTIVE source is synced (today's default,
                  unchanged in the common case). Requires
                  `Authorization: Bearer $SYNC_TOKEN`.
  - GET  /status  per-source last-run summary + top-level {"running": bool}.
  - GET  /health  200 liveness probe.
  - GET  /metrics prometheus-client exposition.

`SYNC_TOKEN` is REQUIRED: the process refuses to start (exits non-zero) if
it is unset, at import time — before uvicorn ever binds a socket. It must
also not be a known placeholder/shipped-default value when any listener is
non-loopback (see `app.security` and `SELF_DOCS_LISTENERS`); on a
loopback-only deployment a placeholder warns instead of refusing.

`doc_sources` (the database) is the sole source of truth for crawl config —
see `app.sources_repo`. There is no `sources.yaml` seed file anymore
(`ingestion/config/` holds only a `.gitkeep` so the Docker build has a
directory to bind-mount) and no boot-time import path: `sources_repo.
import_from_yaml` still exists and is still exercised by
`ingestion/tests/test_sources_repo.py`, but nothing in this module (or
anywhere else) calls it — it is a programmatic-only helper, not wired into
startup. Sources land in `doc_sources` via the admin UI, the MCP
`propose_doc_source` tool (pending-row human approval), or
`scripts/push_sources.py`.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import sys
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from . import admin, metrics, scheduler, security, sources_repo, store, uploads
from .logging_config import get_logger
from .metrics import (
    CHUNKS_INDEXED,
    PAGES_FAILED,
    PAGES_FETCHED,
    PAGES_NOT_MODIFIED,
    PAGES_SHELL_SUSPECTED,
    PAGES_SKIPPED,
    PAGES_SOFT_FAILED,
    SYNC_DURATION,
    SYNC_LAST_STATUS,
    SYNC_LAST_SUCCESS,
)
from .sources_repo import SourceRecord

logger = get_logger(component="main")

# --- Fail fast: SYNC_TOKEN is mandatory -------------------------------------------------
SYNC_TOKEN = os.environ.get("SYNC_TOKEN")
if not SYNC_TOKEN:
    print(
        "FATAL: SYNC_TOKEN environment variable is required but not set. Refusing to start.",
        file=sys.stderr,
    )
    raise SystemExit(1)

# --- Fail fast: a placeholder SYNC_TOKEN must never guard a reachable listener ----------
# Two-branch by design (see app/security.py for the full rationale):
#   loopback-only  + placeholder -> warn loudly, keep booting (don't break local setups)
#   non-loopback   + placeholder -> refuse, before uvicorn binds a socket
# Exposure is declared by the compose layer via SELF_DOCS_LISTENERS, because the
# process itself always binds 0.0.0.0 inside the container and cannot see the
# published-port / Traefik reality. The message never contains the token value.
_token_policy = security.check_shared_token(SYNC_TOKEN, var_name="SYNC_TOKEN")
if _token_policy.should_refuse:
    print(_token_policy.message, file=sys.stderr)
    raise SystemExit(1)
if _token_policy.verdict == "warn":
    print(_token_policy.message, file=sys.stderr)
    logger.warning("sync_token_is_placeholder", listeners_are_loopback_only=True)

# --- Scheduler enable flag ---------------------------------------------------------------
#
# DEFAULT: OFF (disabled unless explicitly opted in via SCHEDULER_ENABLED=true or 1).
# The scheduler must be turned on deliberately (`SCHEDULER_ENABLED=true`) to prevent
# unintended scheduled re-crawling on deployments where manual syncs via `/sync` are preferred.
def _parse_scheduler_enabled(raw: str | None) -> bool:
    """Pure parsing helper, factored out so its truthy/falsy string handling
    is directly unit-testable without needing a subprocess reimport of this
    module (unlike `SYNC_TOKEN`, this flag has no fail-fast side effect at
    import time, so there is otherwise nothing to `assert` against besides
    the module-level constant itself)."""
    return (raw or "").strip().lower() in ("1", "true", "yes")


SCHEDULER_ENABLED = _parse_scheduler_enabled(os.environ.get("SCHEDULER_ENABLED"))


class SourcesUnavailable(RuntimeError):
    """Raised by `get_sources()`/`get_sources_by_name()` when `doc_sources`
    could not be read on this call (DB unreachable, query failed, ...).

    This is purely a connectivity/availability failure — distinct from the
    old `ConfigError` (which meant "sources.yaml has a schema problem" back
    when the yaml file was authoritative). Request handlers turn this into
    an HTTP 503, not a 400.
    """


# --- Load doc_sources at startup: fail FAST if the database is unreachable -------------
# The database (not sources.yaml) is now the source of truth for crawl
# config (see sources_repo.py). Every row in `doc_sources` was already
# validated by `SourceConfig` at whatever write path put it there
# (create_source/update_source/import_from_yaml all funnel through it) — so
# there is nothing left to *validate* at boot beyond "can we actually reach
# the database and read the table". If not, refuse to start rather than
# boot into a service that would fail every request.
def _load_initial_sources() -> list[SourceRecord]:
    conn = store.get_connection()
    try:
        return sources_repo.list_sources(conn)
    finally:
        conn.close()


try:
    _INITIAL_SOURCES: list[SourceRecord] = _load_initial_sources()
except Exception as e:  # noqa: BLE001 - any DB/connectivity failure is fatal at boot
    print(f"FATAL: could not load sources from the database: {e}", file=sys.stderr)
    raise SystemExit(1) from e

# --- Database is sole source of truth for crawl config -----------------------------
# doc_sources (the database) is the source of truth for crawl config (see sources_repo.py).
# All source management occurs via Postgres and the admin API.

# --- Dynamic re-read with a last-known-good cache for test observability ----------------
# Startup (above) is fail-fast: an unreachable database at boot aborts the
# process before uvicorn binds. Runtime re-reads (below) must fail *soft*: a
# bad read while the service is running (DB restart, network blip, a typo'd
# manual row edit, ...) must never crash it or empty out its source list
# mid-flight — callers get a `SourcesUnavailable`, which request handlers
# turn into an HTTP 503, and the process keeps running. `_last_good_sources`
# records the last successfully-loaded config for tests to assert against;
# it is not read on any production request path.
_sources_cache_lock = threading.Lock()
_last_good_sources: list[SourceRecord] = _INITIAL_SOURCES


def get_sources() -> list[SourceRecord]:
    """Re-read ALL `doc_sources` rows (any status) from the database on
    every call.

    On success, updates `_last_good_sources` and returns the fresh list. On
    failure, logs a structured event and raises `SourcesUnavailable`
    WITHOUT touching the cache — the previous last-known-good list is left
    untouched. `_last_good_sources` is a test-observable record only: no
    production call path reads it back as a fallback. The fail-soft
    behavior actually observed (service stays up, caller gets a 503) comes
    from the try/except around this call in the `/sync` handler, not from
    this cache.
    """
    global _last_good_sources
    try:
        conn = store.get_connection()
        try:
            sources = sources_repo.list_sources(conn)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 - any DB failure at request time is fail-soft
        logger.error("sources_db_reload_failed", error=str(e))
        raise SourcesUnavailable(str(e)) from e
    with _sources_cache_lock:
        _last_good_sources = sources
    return sources


def get_sources_by_name() -> dict[str, SourceRecord]:
    """Same contract as `get_sources()`, keyed by name."""
    return {s.name: s for s in get_sources()}


def _get_last_good_sources_by_name() -> dict[str, SourceRecord]:
    """The last successfully-loaded config, without attempting a re-read.

    Not called from any request path — this is a test seam used to assert
    that the fail-soft cache survives a failed reload without triggering
    another `get_sources()` call (which would just re-raise the same
    `SourcesUnavailable`). Kept private (`_`-prefixed) since it has no
    production call site."""
    with _sources_cache_lock:
        return {s.name: s for s in _last_good_sources}


# --- Unified sync lock -------------------------------------------------------------------
#
# THE lock: a single `threading.Lock`, shared by all three sync entrypoints
# (`POST /sync`, the admin manual-sync route, and the scheduler). Before this
# task these were THREE independent, non-cooperating mechanisms:
#   1. `POST /sync`      -> this module's own `asyncio.Lock`
#   2. admin manual sync -> `admin._manual_sync_lock`, a *different*
#                            `threading.Lock` instance
#   3. the scheduler     -> `scheduler.is_sync_locked_fn`, an unwired seam
#                            that always reported "not locked"
# — meaning any two of the three could run a sync concurrently against the
# same source, corrupting `_delete_missing_pages`'s purge accounting (see
# scheduler.py's and this task's dispatch for the calibrated PERMIT/REFUSE
# ratios that depend on a single, uninterrupted `seen_urls` set per source).
#
# CHOICE: `threading.Lock`, not `asyncio.Lock`, as the ONE shared primitive.
# Rationale:
#   - `admin.py`'s routes are sync `def`s run in Starlette's worker thread
#     pool — an `asyncio.Lock` is not safe to acquire/release from a thread
#     that isn't running the event loop (no cross-thread guarantees), so the
#     lock had to move to `threading`, not the other way around.
#   - A `threading.Lock` is perfectly safe to use from the event-loop thread
#     too, AS LONG AS every acquire is non-blocking (`acquire(blocking=False)`)
#     — a blocking acquire would freeze the event loop. Every call site here
#     uses the non-blocking form, so the same lock instance serves the sync
#     `/sync`+scheduler async code and admin's sync route without special-
#     casing either.
#   - `.release()` on a plain `threading.Lock` (unlike `RLock`) is not
#     restricted to the thread that acquired it, which matters because
#     `/sync`'s acquire happens on the request-handling
#     thread/event-loop-turn but its release happens inside a worker thread
#     via `asyncio.to_thread` (`_sync_task`).
#
# WIRING (breaks the circular import `admin.py` flagged): `admin.py` and
# `scheduler.py` each expose their own lock-related seam (`admin.
# try_acquire_sync_lock`/`release_sync_lock`, `scheduler.is_sync_locked_fn`)
# defaulting to a private, module-local fallback so each stays importable
# and independently testable with NO knowledge of `app.main`. Since
# `app.main` already imports both `admin` and `scheduler` (to mount the
# router and start the scheduler task), and neither of THEM imports
# `app.main`, there is no cycle: `main.py` simply rebinds those seams, once,
# at the bottom of this module (see "Startup wiring" below) to route through
# `_sync_lock` below instead of each module's private default.
_sync_lock = threading.Lock()


def try_acquire_sync_lock() -> bool:
    """Non-blocking acquire of the ONE process-wide sync lock shared by all
    three entrypoints. Returns True if acquired — caller must call
    `release_sync_lock()` exactly once when done — or False if a sync from
    ANY of the three paths is already running."""
    return _sync_lock.acquire(blocking=False)


def release_sync_lock() -> None:
    """Release the shared lock. Guarded by `.locked()` so calling this when
    nothing is held (a defensive no-op, e.g. in an error-cleanup path) never
    raises `RuntimeError: release unlocked lock`."""
    if _sync_lock.locked():
        _sync_lock.release()


def is_sync_locked() -> bool:
    """Non-blocking peek at the shared lock's state — used for the fast-fail
    check at the top of `/sync` and wired into
    `scheduler.is_sync_locked_fn` for its pre-trigger "skipped-locked" log
    line. The actual mutual-exclusion guarantee comes from the atomic
    `try_acquire_sync_lock()` acquire, not from this peek (which is
    inherently racy against a concurrent acquire) — every call site that
    needs a real guarantee uses `try_acquire_sync_lock()`, not this."""
    return _sync_lock.locked()


# --- Request body size cap --------------------------------------------------------------
#
# Pure ASGI middleware (not `BaseHTTPMiddleware`): the 413 is sent directly
# via `send()` rather than raised as an exception through the app stack,
# because an exception raised out of `receive()` would otherwise be caught
# by Starlette's outermost `ServerErrorMiddleware` (added automatically,
# always the OUTERMOST wrapper regardless of `add_middleware` call order)
# and turned into a generic 500 -- not the 413 this middleware exists to
# produce. Registered below via `app.add_middleware(...)`, which accepts
# any `cls(app, **options)`-shaped ASGI callable, not just
# `BaseHTTPMiddleware` subclasses.
#
# Two-path enforcement of `uploads.MAX_UPLOAD_BYTES`:
#   1. `Content-Length` header over the limit -> reject with 413
#      IMMEDIATELY, before `receive()` is ever called (the body is never
#      read at all in this path).
#   2. No usable `Content-Length` (missing, or chunked transfer-encoding
#      the ASGI server has already stripped down to a stream of
#      `http.request` messages) -> accumulate the `body` bytes off each
#      ASGI message as they arrive and reject with 413 the moment the
#      running total exceeds the limit, WITHOUT waiting for the rest of
#      the stream. This is the actual bypass this middleware closes: a
#      Content-Length-only check is trivially defeated by omitting the
#      header.
# When the body turns out to be within bounds, the `http.request` messages
# consumed here (to measure it) are buffered and replayed verbatim via
# `buffered_receive` so the wrapped app/route sees an identical stream.
#
# Scoped to POST/PUT/PATCH only: any other method (GET/HEAD/DELETE/...) or
# non-"http" ASGI scope (lifespan, websocket) returns through `self.app`
# on the very first line, with zero header parsing or receive-wrapping --
# `GET /health` and `GET /metrics` never enter the size-check branch at
# all.
class MaxBodySizeMiddleware:
    def __init__(self, app, max_bytes: int = uploads.MAX_UPLOAD_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = None
            if declared is not None and declared > self.max_bytes:
                await self._reject(send)
                return

        # Stream/accumulate: covers both "no Content-Length at all" and a
        # Content-Length that lied (understated the true body size) -- the
        # running total, not the declared header, is what's enforced here.
        buffered: list[dict] = []
        total = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] != "http.request":
                break
            total += len(message.get("body", b"") or b"")
            if total > self.max_bytes:
                await self._reject(send)
                return
            if not message.get("more_body", False):
                break

        index = 0

        async def buffered_receive():
            nonlocal index
            if index < len(buffered):
                message = buffered[index]
                index += 1
                return message
            return await receive()

        await self.app(scope, buffered_receive, send)

    @staticmethod
    async def _reject(send) -> None:
        body = json.dumps({"detail": "request body too large"}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})


app = FastAPI(title="self-docs ingestion")
app.add_middleware(MaxBodySizeMiddleware, max_bytes=uploads.MAX_UPLOAD_BYTES)

_state: dict = {
    "running": False,
    "current": None,
    "results": {},  # source name -> summary dict
}

# --- Prometheus metrics ------------------------------------------------------------------
# Metric OBJECTS and the recording logic both live in `app.metrics` (a leaf
# module with no dependency on `store` or `main`) so that `admin.py`'s sync
# paths -- which must never import `main.py` (circular) -- can record the
# same samples via `store.sync_source_with_metrics` without main.py being
# the only place able to move these counters. Re-exported here under the
# same names so existing call sites (`main.PAGES_FETCHED`, the `/metrics`
# route via `generate_latest()`'s default registry) are unchanged.


class SyncRequest(BaseModel):
    sources: list[str] | None = None
    # Single-source sync target, by `doc_sources.id` (int) or `name` (str) —
    # used by the admin UI's manual-sync button. Mutually exclusive with
    # `sources`; if both are given, `source` wins. With neither set, the
    # default is "every ACTIVE source" (see `_resolve_sync_targets`).
    source: int | str | None = None


class PurgeRequest(BaseModel):
    source: int | str


def _resolve_single_source(
    source_ref: int | str,
    sources_by_name: dict[str, SourceRecord],
    sources_by_id: dict[int, SourceRecord],
) -> SourceRecord:
    record = (
        sources_by_id.get(source_ref)
        if isinstance(source_ref, int)
        else sources_by_name.get(source_ref)
    )
    if record is None:
        if isinstance(source_ref, str):
            try:
                record = sources_by_id.get(int(source_ref))
            except ValueError:
                pass
    if record is None:
        raise HTTPException(status_code=404, detail=f"source {source_ref!r} not found")
    return record


def _check_auth(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="unauthorized")
    token = authorization[len("Bearer "):]
    if not hmac.compare_digest(token, SYNC_TOKEN):
        raise HTTPException(status_code=401, detail="unauthorized")


def _resolve_sync_targets(
    req: SyncRequest | None,
    sources_by_name: dict[str, SourceRecord],
    sources_by_id: dict[int, SourceRecord],
) -> list[str]:
    """Given the request body and the source set resolved ONCE for this
    request (see the `/sync` handler), return the list of source names to
    sync. Raises `HTTPException` for every rejection case so the handler
    itself stays a thin dispatch.

    SECURITY GATE: a source whose `status != 'active'` is refused here with
    a distinct, identifiable error — this is the server-side enforcement
    point for the MCP-proposal approval flow. A source proposed via MCP
    lands as `status='pending'` and must be uncrawlable via `/sync` until a
    human approves it, regardless of whether it's targeted by id, by name,
    or swept up in an unscoped "sync everything" call.
    """
    if req is not None and req.source is not None:
        record = (
            sources_by_id.get(req.source)
            if isinstance(req.source, int)
            else sources_by_name.get(req.source)
        )
        if record is None:
            raise HTTPException(status_code=404, detail=f"unknown source: {req.source!r}")
        if record.status != "active":
            raise HTTPException(
                status_code=403,
                detail=(
                    f"source {record.name!r} has status={record.status!r} (not 'active') "
                    "and cannot be synced until approved"
                ),
            )
        return [record.name]

    if req is not None and req.sources:
        names = req.sources
        unknown = [n for n in names if n not in sources_by_name]
        if unknown:
            raise HTTPException(status_code=400, detail=f"unknown source(s): {unknown}")
        inactive = [n for n in names if sources_by_name[n].status != "active"]
        if inactive:
            raise HTTPException(
                status_code=403,
                detail=f"source(s) not active (status != 'active') and cannot be synced: {inactive}",
            )
        return names

    # Default: every ACTIVE source. A pending/rejected source is never swept
    # up in an unscoped "sync everything" call.
    return [name for name, record in sources_by_name.items() if record.status == "active"]


def _run_sync_blocking(names: list[str], sources_by_name: dict[str, SourceRecord]) -> None:
    """Synchronous sync worker — runs in a worker thread via `asyncio.to_thread`
    so the event loop stays responsive for /status, /health, /metrics.

    `sources_by_name` is resolved ONCE by the caller (the /sync endpoint) and
    passed down here so a concurrent doc_sources edit landing mid-sync
    cannot change the set of sources (or their config) being iterated
    partway through."""
    orig_crawl = store.crawler.crawl

    def tracking_crawl(src, *args, **kwargs):
        for page in orig_crawl(src, *args, **kwargs):
            if _state.get("current") and _state["current"].get("source") == src.name:
                _state["current"]["pages_processed"] += 1
            yield page

    store.crawler.crawl = tracking_crawl
    admin._sync_cancel_event.clear()
    admin._sync_status["running"] = True
    admin._sync_status["started_at"] = time.time()
    admin._sync_status["completed_at"] = None
    admin._sync_status["pages_fetched"] = 0
    admin._sync_status["chunks_indexed"] = 0
    admin._sync_status["pages_skipped"] = 0
    admin._sync_status["pages_failed"] = 0
    admin._sync_status["last_url"] = ""
    admin._sync_status["message"] = f"Background sync running ({len(names)} sources)..."
    try:
        for i, name in enumerate(names):
            log = logger.bind(source=name)
            if admin._sync_cancel_event.is_set():
                log.info("sync_skipped_due_to_cancellation")
                outcome = store.SourceOutcome(name=name, status="failed", error="Aborted by user")
                _state["results"][name] = {
                    "pages_fetched": 0,
                    "pages_skipped": 0,
                    "pages_not_modified": 0,
                    "pages_failed": 0,
                    "pages_soft_failed": 0,
                    "shell_suspected_count": 0,
                    "pages_js_rendered": 0,
                    "pages_removed": 0,
                    "chunks_indexed": 0,
                    "last_status": "failed",
                    "last_synced": time.time(),
                    "error": "Aborted by user",
                }
                continue

            source = sources_by_name[name]
            start = time.monotonic()
            _state["current"] = {
                "source": name,
                "pages_processed": 0,
                "start_time": start,
                "position": f"{i + 1} of {len(names)}",
            }
            admin._sync_status["source"] = name
            admin._sync_status["message"] = f"Syncing {name} ({i + 1} of {len(names)})..."
            # `store.sync_source_with_metrics` is the single instrumented
            # entrypoint every real sync path now routes through (see
            # `store.py` and `app.metrics`) -- it records all seven
            # Prometheus samples itself, so this handler only needs to cover
            # the two failure modes that never even reach it: a dead
            # connection, and `sync_source` crashing outright.
            try:
                conn = store.get_connection()
            except Exception as e:  # noqa: BLE001
                log.error("sync_db_connect_failed", error=str(e))
                outcome = store.SourceOutcome(name=name, status="failed", error=str(e))
                metrics.record_sync_outcome(name, outcome, time.monotonic() - start)
            else:
                try:
                    cfg = admin._record_to_config(source)
                    outcome = store.sync_source_with_metrics(
                        cfg, conn, progress_cb=admin._on_sync_progress, cancel_event=admin._sync_cancel_event
                    )
                except Exception as e:  # noqa: BLE001 - source-level safety net
                    log.error("sync_source_crashed", error=str(e))
                    outcome = store.SourceOutcome(name=name, status="failed", error=str(e))
                    metrics.record_sync_outcome(name, outcome, time.monotonic() - start)
                    # `conn` may be the reason we're here (e.g. a dropped
                    # connection), so `sync_source` never reached its own
                    # `_update_source_status` call — without this, doc_sources
                    # would show last_status=NULL, indistinguishable from "never
                    # ran". Best-effort on a fresh connection; never raises.
                    store.mark_source_failed(name)
                finally:
                    conn.close()

            _state["results"][name] = {
                "pages_fetched": outcome.pages_fetched,
                "pages_skipped": outcome.pages_skipped,
                "pages_not_modified": outcome.pages_not_modified,
                "pages_failed": outcome.pages_failed,
                "pages_soft_failed": outcome.pages_soft_failed,
                "shell_suspected_count": outcome.shell_suspected_count,
                "pages_js_rendered": outcome.pages_js_rendered,
                "pages_removed": outcome.pages_removed,
                "chunks_indexed": outcome.chunks_indexed,
                "last_status": outcome.status,
                "last_synced": time.time(),
                "error": outcome.error,
            }
    finally:
        store.crawler.crawl = orig_crawl
        admin._sync_status["running"] = False
        admin._sync_status["source"] = ""
        admin._sync_status["started_at"] = None
        admin._sync_status["completed_at"] = time.time()
        admin._sync_status["message"] = ""
        total_fetched = sum(r.get("pages_fetched", 0) for r in _state["results"].values())
        total_chunks = sum(r.get("chunks_indexed", 0) for r in _state["results"].values())
        total_skipped = sum(r.get("pages_skipped", 0) for r in _state["results"].values())
        total_not_modified = sum(r.get("pages_not_modified", 0) for r in _state["results"].values())
        total_failed = sum(r.get("pages_failed", 0) + r.get("pages_soft_failed", 0) for r in _state["results"].values())
        total_shell_suspected = sum(r.get("shell_suspected_count", 0) for r in _state["results"].values())
        total_js_rendered = sum(r.get("pages_js_rendered", 0) for r in _state["results"].values())
        any_failed = any(r.get("last_status") == "failed" for r in _state["results"].values())
        errors = [str(r["error"]) for r in _state["results"].values() if r.get("error")]
        admin._sync_status["last_completed_summary"] = {
            "source": f"Background Sync ({len(names)} sources)",
            "status": "failed" if any_failed else "ok",
            "pages_fetched": total_fetched,
            "chunks_indexed": total_chunks,
            "pages_skipped": total_skipped,
            "pages_not_modified": total_not_modified,
            "pages_failed": total_failed,
            "shell_suspected_count": total_shell_suspected,
            "pages_js_rendered": total_js_rendered,
            "error": "; ".join(errors) if errors else None,
            "finished_at": time.time(),
        }


async def _sync_task(names: list[str], sources_by_name: dict[str, SourceRecord]) -> None:
    try:
        await asyncio.to_thread(_run_sync_blocking, names, sources_by_name)
    finally:
        _state["running"] = False
        _state["current"] = None
        release_sync_lock()


async def _scheduler_trigger_sync(names: list[str]) -> None:
    """Wired into `scheduler.trigger_sync_fn` at startup (see "Startup
    wiring" below) — the SAME source-resolution and worker
    (`_run_sync_blocking`) that `POST /sync` uses, routed through the SAME
    unified lock. The one behavioral difference from `POST /sync` is that
    this AWAITS the sync to completion before returning rather than firing a
    background task: `scheduler.run_scheduler` processes one due source at a
    time and awaits this call before moving on, so there is no need (and no
    caller) for a fire-and-forget shape here.

    Acquires the lock itself (rather than trusting the caller's prior
    `is_sync_locked_fn()` peek) because that peek is inherently racy against
    a concurrent `/sync` or admin manual-sync call landing in the gap
    between the peek and this call — this is the atomic checkpoint that
    actually prevents an interleaved crawl.
    """
    if not try_acquire_sync_lock():
        logger.info("scheduler_sync_skipped_locked", sources=names)
        return
    try:
        try:
            all_sources = get_sources()
        except SourcesUnavailable as e:
            logger.error("scheduler_sync_sources_unavailable", error=str(e), sources=names)
            return

        sources_by_name = {s.name: s for s in all_sources}
        missing = [n for n in names if n not in sources_by_name]
        if missing:
            logger.error("scheduler_sync_unknown_sources", missing=missing, sources=names)
            return

        _state["running"] = True
        _state["current"] = {
            "source": names[0] if names else "",
            "pages_processed": 0,
            "start_time": time.monotonic(),
            "position": f"1 of {len(names)}" if names else "0 of 0",
        }
        try:
            await asyncio.to_thread(_run_sync_blocking, names, sources_by_name)
        finally:
            _state["running"] = False
            _state["current"] = None
    finally:
        release_sync_lock()


# --- Startup wiring: unify the three sync entrypoints behind one lock --------------------
#
# Runs once, at import time (module-level, not inside a request/lifespan
# handler) — before any request can possibly reach `admin.router`'s routes
# or `scheduler.run_scheduler` can possibly be started, so there is no
# window where an unwired seam could be hit.
admin.try_acquire_sync_lock = try_acquire_sync_lock
admin.release_sync_lock = release_sync_lock
scheduler.fetch_candidates_fn = get_sources
scheduler.trigger_sync_fn = _scheduler_trigger_sync
scheduler.is_sync_locked_fn = is_sync_locked

# Admin UI: mounted at `/admin`. Every route in `admin.router` is already
# auth-gated internally (via `admin.require_session` / `admin.require_csrf`
# — see admin.py's module docstring); including the router here does not
# bypass that, it only makes the routes reachable at all.
app.include_router(admin.router)


# --- Scheduler lifespan wiring ------------------------------------------------------------
#
# Started/stopped as a FastAPI lifespan task rather than a bare
# fire-and-forget `asyncio.create_task` at import time, so shutdown is
# deterministic and prompt: `stop_event.set()` + `await task` here relies on
# `run_scheduler`'s own `asyncio.wait_for(stop.wait(), timeout=...)` sleep
# (see scheduler.py), which returns immediately on `stop.set()` instead of
# blocking for up to `poll_interval_seconds` — a scheduler that blocks
# shutdown turns every deploy into a SIGKILL wait.
_scheduler_task: asyncio.Task | None = None
_scheduler_stop_event: asyncio.Event | None = None


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _scheduler_task, _scheduler_stop_event
    if SCHEDULER_ENABLED:
        _scheduler_stop_event = asyncio.Event()
        _scheduler_task = asyncio.create_task(scheduler.run_scheduler(_scheduler_stop_event))
        logger.info("scheduler_task_started")
    else:
        logger.info("scheduler_disabled", reason="SCHEDULER_ENABLED not set/true")
    try:
        yield
    finally:
        if _scheduler_task is not None:
            _scheduler_stop_event.set()
            await _scheduler_task
            logger.info("scheduler_task_stopped")
            _scheduler_task = None
            _scheduler_stop_event = None


app.router.lifespan_context = _lifespan


@app.post("/sync")
async def sync(req: SyncRequest | None = None, authorization: str | None = Header(default=None)):
    _check_auth(authorization)

    if is_sync_locked():
        raise HTTPException(status_code=409, detail="sync already running")

    # Resolve the source set ONCE for this request: a re-read here fails
    # soft (this try/except turns a `SourcesUnavailable` into a 503 and the
    # service keeps running) rather than crashing — unlike the fail-fast
    # startup load above. Everything downstream (id/name lookup, the
    # active-status gate, and the actual sync) is threaded from this single
    # snapshot so a concurrent doc_sources edit can't change the set being
    # iterated mid-sync.
    try:
        all_sources = get_sources()
    except SourcesUnavailable as e:
        raise HTTPException(status_code=503, detail=f"sources unavailable: {e}") from e

    sources_by_name = {s.name: s for s in all_sources}
    sources_by_id = {s.id: s for s in all_sources}

    names = _resolve_sync_targets(req, sources_by_name, sources_by_id)

    # Atomic re-check-and-acquire: the `is_sync_locked()` check above is a
    # fast-fail peek taken BEFORE the (potentially slow) source resolution
    # above; a concurrent request could acquire the lock in that window, so
    # the actual mutual-exclusion guarantee comes from this non-blocking
    # `try_acquire_sync_lock()`, not the earlier peek.
    if not try_acquire_sync_lock():
        raise HTTPException(status_code=409, detail="sync already running")

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


@app.post("/purge")
async def purge(req: PurgeRequest, authorization: str | None = Header(default=None)):
    _check_auth(authorization)

    if is_sync_locked():
        raise HTTPException(status_code=409, detail="sync already running")

    try:
        all_sources = get_sources()
    except SourcesUnavailable as e:
        raise HTTPException(status_code=503, detail=f"sources unavailable: {e}") from e

    sources_by_name = {s.name: s for s in all_sources}
    sources_by_id = {s.id: s for s in all_sources}

    record = _resolve_single_source(req.source, sources_by_name, sources_by_id)

    if not try_acquire_sync_lock():
        raise HTTPException(status_code=409, detail="sync already running")

    try:
        conn = store.get_connection()
        try:
            count = store.purge_source(conn, record.id)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to purge source: {e}") from e
    finally:
        release_sync_lock()

    logger.info("source_purged", source=record.name, pages_deleted=count)
    return {"purged_pages": count, "source": record.name}


@app.post("/refresh")
async def refresh(req: PurgeRequest, authorization: str | None = Header(default=None)):
    _check_auth(authorization)

    if is_sync_locked():
        raise HTTPException(status_code=409, detail="sync already running")

    try:
        all_sources = get_sources()
    except SourcesUnavailable as e:
        raise HTTPException(status_code=503, detail=f"sources unavailable: {e}") from e

    sources_by_name = {s.name: s for s in all_sources}
    sources_by_id = {s.id: s for s in all_sources}

    record = _resolve_single_source(req.source, sources_by_name, sources_by_id)

    if record.status != "active":
        raise HTTPException(
            status_code=403,
            detail=f"source {record.name!r} has status={record.status!r} (not 'active') and cannot be synced"
        )

    if not try_acquire_sync_lock():
        raise HTTPException(status_code=409, detail="sync already running")

    try:
        conn = store.get_connection()
        try:
            count = store.purge_source(conn, record.id)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        release_sync_lock()
        raise HTTPException(status_code=500, detail=f"failed to purge source: {e}") from e

    _state["running"] = True
    _state["current"] = {
        "source": record.name,
        "pages_processed": 0,
        "start_time": time.monotonic(),
        "position": "1 of 1",
    }
    logger.info("refresh_sync_started", source=record.name)
    asyncio.create_task(_sync_task([record.name], sources_by_name))

    return {"purged_pages": count, "sync": "started", "source": record.name}


@app.post("/stop")
async def stop(authorization: str | None = Header(default=None)):
    _check_auth(authorization)
    admin._sync_cancel_event.set()
    logger.info("api_stop_triggered")
    return {"sync": "stopping"}


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
async def metrics_endpoint():
    # NOTE: deliberately not named `metrics` -- that name is the module-level
    # `from . import metrics` import (the shared Prometheus objects +
    # `record_sync_outcome` helper in `app/metrics.py`); a route function of
    # the same name would rebind the module-global `metrics` identifier to
    # this coroutine for the rest of the file, breaking every later
    # `metrics.record_sync_outcome(...)` call with an `AttributeError`.
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# --- REST API Endpoints for doc-cli & HTTP Clients ----------------------------------------
@app.get("/api/v1/search")
async def api_search(
    q: str,
    source: str | None = None,
    limit: int = 5,
    authorization: str | None = Header(default=None),
):
    """Hybrid RRF search returning token-optimized chunk snippets for doc-cli."""
    _check_auth(authorization)
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="search query 'q' cannot be empty")
    clamped_limit = min(max(limit, 1), 50)
    conn = store.get_connection()
    try:
        results = store.search_chunks(conn, query=q.strip(), source=source, limit=clamped_limit)
        return results
    finally:
        conn.close()


@app.get("/api/v1/chunks/{chunk_id}")
async def api_get_chunk(
    chunk_id: int,
    authorization: str | None = Header(default=None),
):
    """Retrieve full text markdown body and metadata for a specific chunk ID."""
    _check_auth(authorization)
    conn = store.get_connection()
    try:
        chunk = store.get_chunk_by_id(conn, chunk_id)
        if chunk is None:
            raise HTTPException(status_code=404, detail=f"chunk ID {chunk_id} not found")
        return chunk
    finally:
        conn.close()


@app.get("/api/v1/tree")
async def api_get_tree(
    authorization: str | None = Header(default=None),
):
    """Retrieve indexed source hierarchy with page and chunk totals."""
    _check_auth(authorization)
    conn = store.get_connection()
    try:
        return store.get_source_tree(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8080)

