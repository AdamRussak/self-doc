"""Server-rendered admin UI for managing `doc_sources` (Jinja2 + vendored HTMX).

Mount point: `router = APIRouter(prefix="/admin")` — a self-contained
`APIRouter` intended to be included by `app.main` (wired in by a sibling
task; this module never imports `app.main` to avoid a circular import).

Routes (see module docstring sections below for detail):

    GET  /admin                        list active + pending sources
    GET  /admin/sources/new            create form
    POST /admin/sources/new            create
    GET  /admin/sources/{id}           edit form
    POST /admin/sources/{id}           update (config + schedule + enabled)
    POST /admin/sources/{id}/delete    delete (cascades pages+chunks)
    POST /admin/sources/{id}/sync      manual sync trigger
    POST /admin/sources/{id}/upload    upload files (source_type='upload' only)
    POST /admin/sources/{id}/approve   pending -> active
    POST /admin/sources/{id}/reject    pending -> rejected
    GET  /admin/login                  login form (unauthenticated)
    POST /admin/login                  exchange SYNC_TOKEN for a session cookie

Auth model (session cookie + CSRF token) — see the "Auth" section below for
the full rationale and the concrete tradeoff versus staying bearer-only.

Every `doc_sources` write on this router validates the submitted form data
through `app.config.SourceConfig` (or, for schedule_cron,
`sources_repo.validate_cron`) BEFORE touching the database. Nothing is ever
partially applied: config, schedule, and enabled-state are all validated
first; only if everything validates do we call into `sources_repo`.
"""

from __future__ import annotations

import hmac
import os
import threading
import time
from collections.abc import Callable, Collection
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse

import psycopg
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from . import sources_repo, store, upload_zip, uploads
from .config import SUPPORTED_FTS_LANGUAGES, ConfigError, SourceConfig
from .logging_config import get_logger
from .source_defaults import apply_creation_defaults
from .sources_repo import SourceRecord
from .uploads import UploadedDoc, UploadError

logger = get_logger(component="admin")

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = (Path(__file__).parent / "static").resolve()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/admin")

_STATIC_MEDIA_TYPES = {".js": "application/javascript", ".css": "text/css"}

# --- Auth --------------------------------------------------------------------------------
#
# CHOICE: session cookie (HMAC-signed, derived from SYNC_TOKEN) + a
# synchronizer CSRF token, NOT bearer-only.
#
# Bearer-only was rejected for THIS surface because "form posts must work
# from a browser": a plain HTML <form> cannot attach an `Authorization`
# header, so bearer-only would force the operator into a browser extension,
# a bookmarklet, or a non-browser client (curl/httpie) to drive an admin UI
# that is explicitly supposed to be clicked around in a browser. That
# defeats the point of building a UI at all.
#
# So: GET/POST /admin/login exchanges the existing `SYNC_TOKEN` (compared
# with `hmac.compare_digest`, same as `main._check_auth`) for an
# httponly + Secure + SameSite=Lax session cookie. The cookie VALUE is
# `f"{issued_at}.{HMAC-SHA256(SYNC_TOKEN, f'session-v1:{issued_at}')}"` — a
# value only a process that knows `SYNC_TOKEN` can compute — never the raw
# token itself, so a leaked cookie (log line, browser history, XSS) does not
# directly disclose `SYNC_TOKEN` (only a rotation-scoped equivalent that dies
# the moment the token is rotated).
#
# EXPIRY (added after security review M2): the issued-at timestamp is bound
# INTO the HMAC message, not merely appended alongside it — so editing the
# timestamp without knowing `SYNC_TOKEN` invalidates the digest, and the
# cookie is rejected outright by `_is_authenticated` (see there) rather than
# silently trusting a forged expiry. A session older than
# `SESSION_MAX_AGE_SECONDS` is rejected even though the digest still checks
# out, giving revocation-by-time without a server-side session store. 7 days
# was chosen as the max age: long enough that an operator doing routine
# source-review/approval work isn't forced to re-enter `SYNC_TOKEN` every
# session, short enough to bound how long a stolen cookie (e.g. captured by
# another container reaching `http://ingestion:8080/admin` over the shared
# `self-docs-internal` Docker network) stays usable without a full
# `SYNC_TOKEN` rotation.
#
# `Secure` is set on the cookie even though this service is published only
# on `127.0.0.1`: Chrome and Firefox both treat `http://127.0.0.1` (and
# `http://localhost`) as a "secure context" for cookie purposes, so this
# costs nothing in the current deployment, and it is the difference between
# safe and unsafe the moment this ever moves behind a reverse proxy (e.g.
# Traefik) that isn't terminating strictly loopback-only TLS.
#
# Every state-changing (POST) route additionally requires a hidden
# `csrf_token` form field equal to `HMAC-SHA256(SYNC_TOKEN, "csrf-v1")`,
# rendered into every form the templates emit. A cross-origin attacker
# forging a POST from another page cannot supply this value: they don't
# know `SYNC_TOKEN`, and same-origin policy stops them reading it out of an
# authenticated response even if the browser were to attach the session
# cookie to their forged request. `SameSite=Lax` on the cookie is defense
# in depth on top of that (blocks the cookie from being attached to a
# cross-site POST at all in compliant browsers).
#
# TRADEOFF (stated plainly): both the session value and the CSRF token are
# DETERMINISTIC functions of `SYNC_TOKEN`, not per-login random nonces —
# there is no server-side session store here (this service is otherwise
# stateless). That means every login shares the same cookie/CSRF pair until
# `SYNC_TOKEN` is rotated; a captured cookie remains valid until then
# (rotation is the only revocation mechanism). A production hardening step
# would swap this for a random per-session token in a small session store
# (or a signed, time-boxed JWT) — out of scope here, flagged for review.
SESSION_COOKIE = "admin_session"

# See the EXPIRY note in the module docstring above for why 7 days.
SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


def _sync_token() -> str | None:
    """Read `SYNC_TOKEN` at call time (not import time): this module must
    not require `SYNC_TOKEN` to be set in order to be *imported* (tests
    import it standalone, without the fail-fast startup check `app.main`
    performs). Every route that needs it treats a missing token as
    "nothing can authenticate" rather than raising at import."""
    return os.environ.get("SYNC_TOKEN")


def _sign(purpose: str) -> str:
    token = _sync_token() or ""
    return hmac.new(token.encode("utf-8"), purpose.encode("utf-8"), sha256).hexdigest()


def _session_value_for(issued_at: int) -> str:
    """Build the session cookie value for a given issue timestamp (epoch
    seconds). The timestamp is baked INTO the HMAC message
    (`f"session-v1:{issued_at}"`), not merely concatenated alongside an
    unrelated digest — so a forged/edited `issued_at` invalidates the digest
    rather than silently extending (or backdating) the session."""
    return f"{issued_at}.{_sign(f'session-v1:{issued_at}')}"


def _new_session_value() -> str:
    return _session_value_for(int(time.time()))


def _expected_csrf_token() -> str:
    return _sign("csrf-v1")


def _is_authenticated(request: Request) -> bool:
    token = _sync_token()
    if not token:
        return False
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return False
    issued_at_str, sep, digest = cookie.partition(".")
    if not sep or not issued_at_str.isdigit() or not digest:
        return False
    issued_at = int(issued_at_str)
    expected_digest = _sign(f"session-v1:{issued_at}")
    if not hmac.compare_digest(digest, expected_digest):
        return False
    age_seconds = time.time() - issued_at
    # Reject anything outside [0, MAX_AGE]: a negative age means the
    # timestamp claims to be in the future (only possible if it was forged,
    # since a legitimately-issued cookie's timestamp is always <= now at the
    # moment it's checked); anything past MAX_AGE is an expired session.
    if age_seconds < 0 or age_seconds > SESSION_MAX_AGE_SECONDS:
        return False
    return True


def require_session(request: Request) -> None:
    """Auth dependency for every route below except `/admin/login`. Raises
    401 (not a redirect) so the check is uniformly testable per-route
    without depending on a browser to follow a Location header."""
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="unauthorized: POST your SYNC_TOKEN to /admin/login")


def require_csrf(request: Request, csrf_token: str = Form(default="")) -> None:
    """Auth + CSRF dependency for every state-changing (POST) route. Checks
    session auth first (so an unauthenticated forged POST gets a plain 401,
    not a CSRF-specific 403 that would leak "you're logged in but missing a
    token")."""
    require_session(request)
    if not hmac.compare_digest(csrf_token or "", _expected_csrf_token()):
        raise HTTPException(status_code=403, detail="invalid or missing csrf_token")


# --- Vendored static assets (htmx.js) -----------------------------------------------------
#
# Deliberately NOT `app.mount(...)`/`router.mount(...)` — this codebase's
# installed FastAPI/Starlette resolve `include_router` lazily in a way that
# does not surface a nested `Mount`'s sub-routes (verified by hand: a
# `router.mount("/static", StaticFiles(...))` 404s once included into the
# app). A plain `@router.get` reading the file directly sidesteps that
# entirely and keeps this module self-contained. Requires auth like every
# other route here (see module docstring) — the only unauthenticated route
# is `/admin/login` itself, and the login page is plain HTML/CSS that does
# not need htmx to render or submit.


@router.get("/static/{filename:path}")
def static_asset(filename: str, _auth=Depends(require_session)):
    candidate = (STATIC_DIR / filename).resolve()
    if STATIC_DIR not in candidate.parents or not candidate.is_file():
        raise HTTPException(status_code=404, detail="not found")
    media_type = _STATIC_MEDIA_TYPES.get(candidate.suffix, "application/octet-stream")
    return Response(content=candidate.read_bytes(), media_type=media_type)


# --- DB connection dependency (overridden with a fake in tests) --------------------------


def get_conn():
    """Yields a fresh connection per request, closed afterwards. Tests
    override this via `app.dependency_overrides[admin.get_conn]` and
    monkeypatch the `sources_repo`/`store` functions the routes call, so
    route-level auth/validation/rendering logic gets real coverage with no
    live Postgres (see `tests/test_admin.py`)."""
    conn = store.get_connection()
    try:
        yield conn
    finally:
        conn.close()


# --- Manual sync: a dedicated lock, held for the duration of one source's sync ------------
#
# A plain `threading.Lock` (not `asyncio.Lock`): every route in this module
# is a sync `def`, which Starlette runs in its worker thread pool, so a
# thread-level lock is the correct primitive for mutual exclusion across
# concurrent requests here.
#
# UNIFICATION (task B5): this used to be a lock this module owned outright,
# entirely independent of `app.main`'s `_sync_lock` — which meant a manual
# sync here and a `POST /sync` (or the scheduler) could run concurrently
# against the same source, corrupting `_delete_missing_pages`'s purge
# accounting. `admin.py` still must not import `app.main` at module level
# (that would be circular: `main.py` imports `admin.py` to mount its
# router), so instead of sharing a lock *reference* directly, this module
# exposes the acquire/release as two INJECTABLE SEAMS —
# `try_acquire_sync_lock` / `release_sync_lock` — following the exact same
# pattern `app.scheduler` already uses for its DB/sync/lock seams.
#
# Standalone (this module imported without `app.main`, e.g. by
# `tests/test_admin.py`), the seams default to `_manual_sync_lock` below, so
# this module stays fully self-contained and independently testable. At
# startup, `app.main` rebinds both seams to route through its own process-
# wide lock, so a manual sync, `POST /sync`, and the scheduler all
# mutually exclude each other through the SAME lock.
_manual_sync_lock = threading.Lock()


def _default_try_acquire_lock() -> bool:
    """Default (standalone) lock-acquire seam: a non-blocking acquire of
    this module's own `_manual_sync_lock`. Replaced by `app.main` at startup
    wiring time with a callable that acquires the process-wide unified
    lock instead."""
    return _manual_sync_lock.acquire(blocking=False)


def _default_release_lock() -> None:
    """Default (standalone) lock-release seam — the counterpart to
    `_default_try_acquire_lock`. Replaced by `app.main` at startup wiring
    time alongside it."""
    if _manual_sync_lock.locked():
        _manual_sync_lock.release()


try_acquire_sync_lock: Callable[[], bool] = _default_try_acquire_lock
release_sync_lock: Callable[[], None] = _default_release_lock

_sync_cancel_event = threading.Event()

_sync_status: dict[str, Any] = {
    "running": False,
    "source": "",
    "started_at": None,
    "completed_at": None,
    "message": "",
    "pages_fetched": 0,
    "chunks_indexed": 0,
    "pages_skipped": 0,
    "pages_failed": 0,
    "shell_suspected_count": 0,
    "pages_js_rendered": 0,
    "last_url": "",
    "last_completed_summary": None,
}


def _safe_int(obj: Any, attr: str) -> int:
    val = getattr(obj, attr, 0)
    return val if isinstance(val, int) else 0


def _safe_str(obj: Any, attr: str) -> str | None:
    val = getattr(obj, attr, None)
    return val if isinstance(val, str) else None


def _on_sync_progress(outcome: Any, current_url: str) -> None:
    _sync_status["pages_fetched"] = _safe_int(outcome, "pages_fetched")
    _sync_status["chunks_indexed"] = _safe_int(outcome, "chunks_indexed")
    _sync_status["pages_skipped"] = _safe_int(outcome, "pages_skipped")
    _sync_status["pages_failed"] = _safe_int(outcome, "pages_failed") + _safe_int(outcome, "pages_soft_failed")
    _sync_status["shell_suspected_count"] = _safe_int(outcome, "shell_suspected_count")
    _sync_status["pages_js_rendered"] = _safe_int(outcome, "pages_js_rendered")
    _sync_status["last_url"] = str(current_url)
    _sync_status["message"] = f"Syncing {getattr(outcome, 'name', '')} ({_sync_status['pages_fetched']} indexed, {_sync_status['pages_skipped']} skipped)..."


def _default_run_sync_task(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Default sync runner seam: runs in background thread unless PYTEST_CURRENT_TEST or SYNC_RUNNER_SYNC=1."""
    if os.environ.get("SYNC_RUNNER_SYNC") == "1" or os.environ.get("PYTEST_CURRENT_TEST"):
        fn(*args, **kwargs)
    else:
        threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()


run_sync_task: Callable[..., None] = _default_run_sync_task


def _bg_sync_single(cfg: SourceConfig, conn_factory: Callable[[], Any], source_id: int) -> None:
    """Worker for background single-source sync."""
    _sync_cancel_event.clear()
    _sync_status["running"] = True
    _sync_status["source"] = cfg.name
    _sync_status["started_at"] = time.time()
    _sync_status["completed_at"] = None
    _sync_status["message"] = f"Syncing {cfg.name}..."
    _sync_status["pages_fetched"] = 0
    _sync_status["chunks_indexed"] = 0
    _sync_status["pages_skipped"] = 0
    _sync_status["pages_failed"] = 0
    _sync_status["shell_suspected_count"] = 0
    _sync_status["pages_js_rendered"] = 0
    _sync_status["last_url"] = ""
    outcome: store.SourceOutcome | None = None
    exc_message: str | None = None
    try:
        if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("SYNC_RUNNER_SYNC") == "1":
            conn = conn_factory()
            outcome = store.sync_source_with_metrics(cfg, conn, progress_cb=_on_sync_progress, cancel_event=_sync_cancel_event)
        else:
            conn = store.get_connection()
            try:
                outcome = store.sync_source_with_metrics(cfg, conn, progress_cb=_on_sync_progress, cancel_event=_sync_cancel_event)
            finally:
                conn.close()
        logger.info("admin_manual_sync_complete", source_id=source_id, name=cfg.name, status=outcome.status)
    except Exception as exc:
        exc_message = str(exc)
        logger.error("admin_manual_sync_failed", source_id=source_id, name=cfg.name, error=str(exc))
    finally:
        try:
            _sync_status["running"] = False
            _sync_status["source"] = ""
            _sync_status["started_at"] = None
            _sync_status["completed_at"] = time.time()
            if outcome is not None:
                status_str = _safe_str(outcome, "status") or "ok"
                _sync_status["last_completed_summary"] = {
                    "source": cfg.name,
                    "status": status_str,
                    "pages_fetched": _safe_int(outcome, "pages_fetched"),
                    "chunks_indexed": _safe_int(outcome, "chunks_indexed"),
                    "pages_skipped": _safe_int(outcome, "pages_skipped"),
                    "pages_failed": _safe_int(outcome, "pages_failed") + _safe_int(outcome, "pages_soft_failed"),
                    "shell_suspected_count": _safe_int(outcome, "shell_suspected_count"),
                    "pages_js_rendered": _safe_int(outcome, "pages_js_rendered"),
                    "error": _safe_str(outcome, "error"),
                    "finished_at": time.time(),
                }
            else:
                _sync_status["last_completed_summary"] = {
                    "source": cfg.name,
                    "status": "failed",
                    "pages_fetched": _sync_status.get("pages_fetched", 0),
                    "chunks_indexed": _sync_status.get("chunks_indexed", 0),
                    "pages_skipped": _sync_status.get("pages_skipped", 0),
                    "pages_failed": _sync_status.get("pages_failed", 0) + 1,
                    "shell_suspected_count": _sync_status.get("shell_suspected_count", 0),
                    "pages_js_rendered": _sync_status.get("pages_js_rendered", 0),
                    "error": exc_message or _sync_status.get("message", "Sync failed unexpectedly"),
                    "finished_at": time.time(),
                }
            _sync_status["message"] = ""
        finally:
            release_sync_lock()


def _bg_ingest_upload(record: SourceRecord, docs: list[UploadedDoc], conn_factory: Callable[[], Any], source_id: int) -> None:
    """Worker for background upload ingestion — mirrors `_bg_sync_single`
    exactly, swapping `store.sync_source_with_metrics` for
    `store.ingest_uploaded_docs`. Only reached in production (non-pytest,
    non-`SYNC_RUNNER_SYNC`) requests via `run_sync_task`; the lock acquired
    by the calling route (`upload_source_submit` for the edit page's upload
    form, `create_source_submit` for files attached to a create) before
    handing off to this worker is released here, in the background thread,
    once ingestion completes (or fails)."""
    _sync_status["running"] = True
    _sync_status["source"] = record.name
    _sync_status["started_at"] = time.time()
    _sync_status["completed_at"] = None
    _sync_status["message"] = f"Uploading to {record.name}..."
    _sync_status["pages_fetched"] = 0
    _sync_status["chunks_indexed"] = 0
    _sync_status["pages_skipped"] = 0
    _sync_status["pages_failed"] = 0
    _sync_status["shell_suspected_count"] = 0
    _sync_status["pages_js_rendered"] = 0
    _sync_status["last_url"] = ""
    outcome: store.SourceOutcome | None = None
    exc_message: str | None = None
    try:
        if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("SYNC_RUNNER_SYNC") == "1":
            conn = conn_factory()
            outcome = store.ingest_uploaded_docs(conn, record, docs, progress_cb=_on_sync_progress)
        else:
            conn = store.get_connection()
            try:
                outcome = store.ingest_uploaded_docs(conn, record, docs, progress_cb=_on_sync_progress)
            finally:
                conn.close()
        logger.info("admin_upload_ingest_complete", source_id=source_id, name=record.name, status=outcome.status)
    except Exception as exc:
        exc_message = str(exc)
        logger.error("admin_upload_ingest_failed", source_id=source_id, name=record.name, error=str(exc))
    finally:
        try:
            _sync_status["running"] = False
            _sync_status["source"] = ""
            _sync_status["started_at"] = None
            _sync_status["completed_at"] = time.time()
            if outcome is not None:
                status_str = _safe_str(outcome, "status") or "ok"
                _sync_status["last_completed_summary"] = {
                    "source": record.name,
                    "status": status_str,
                    "pages_fetched": _safe_int(outcome, "pages_fetched"),
                    "chunks_indexed": _safe_int(outcome, "chunks_indexed"),
                    "pages_skipped": _safe_int(outcome, "pages_skipped"),
                    "pages_failed": _safe_int(outcome, "pages_failed") + _safe_int(outcome, "pages_soft_failed"),
                    "shell_suspected_count": _safe_int(outcome, "shell_suspected_count"),
                    "pages_js_rendered": _safe_int(outcome, "pages_js_rendered"),
                    "error": _safe_str(outcome, "error"),
                    "finished_at": time.time(),
                }
            else:
                _sync_status["last_completed_summary"] = {
                    "source": record.name,
                    "status": "failed",
                    "pages_fetched": _sync_status.get("pages_fetched", 0),
                    "chunks_indexed": _sync_status.get("chunks_indexed", 0),
                    "pages_skipped": _sync_status.get("pages_skipped", 0),
                    "pages_failed": _sync_status.get("pages_failed", 0) + 1,
                    "shell_suspected_count": _sync_status.get("shell_suspected_count", 0),
                    "pages_js_rendered": _sync_status.get("pages_js_rendered", 0),
                    "error": exc_message or _sync_status.get("message", "Upload ingestion failed unexpectedly"),
                    "finished_at": time.time(),
                }
            _sync_status["message"] = ""
        finally:
            release_sync_lock()


def _bg_sync_all(sources: list[SourceRecord], conn_factory: Callable[[], Any]) -> None:
    """Worker for background full sync across all active sources.

    `sources` is filtered to exclude `source_type == 'upload'` records
    UP FRONT, before either branch below builds any `SourceConfig` --
    previously a single upload source in the batch made `_record_to_config`
    raise (see its docstring) and aborted the ENTIRE full sync at the
    `except Exception` below, so no crawl source synced at all (T19_FIX).
    This mirrors `store.sync_all`'s own upload-skip guard, but is enforced
    here too since the `PYTEST_CURRENT_TEST`/`SYNC_RUNNER_SYNC` branch below
    calls `sync_source_with_metrics` directly per source rather than going
    through `store.sync_all`, so that guard alone would not have covered it."""
    sources = [s for s in sources if s.source_type != "upload"]
    _sync_cancel_event.clear()
    _sync_status["running"] = True
    _sync_status["source"] = "All Active Sources"
    _sync_status["started_at"] = time.time()
    _sync_status["completed_at"] = None
    _sync_status["message"] = f"Full sync started ({len(sources)} sources)..."
    _sync_status["pages_fetched"] = 0
    _sync_status["chunks_indexed"] = 0
    _sync_status["pages_skipped"] = 0
    _sync_status["pages_failed"] = 0
    _sync_status["shell_suspected_count"] = 0
    _sync_status["pages_js_rendered"] = 0
    _sync_status["last_url"] = ""
    results: dict[str, store.SourceOutcome] | None = None
    exc_message: str | None = None
    try:
        if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("SYNC_RUNNER_SYNC") == "1":
            conn = conn_factory()
            results = {}
            for rec in sources:
                if _sync_cancel_event.is_set():
                    results[rec.name] = store.SourceOutcome(name=rec.name, status="failed", error="Aborted by user")
                    continue
                cfg = _record_to_config(rec)
                results[cfg.name] = store.sync_source_with_metrics(cfg, conn, progress_cb=_on_sync_progress, cancel_event=_sync_cancel_event)
        else:
            cfgs = [_record_to_config(rec) for rec in sources]
            results = store.sync_all(cfgs, progress_cb=_on_sync_progress, cancel_event=_sync_cancel_event)
        logger.info("admin_full_sync_complete", count=len(sources))
    except Exception as exc:
        exc_message = str(exc)
        logger.error("admin_full_sync_failed", error=str(exc))
    finally:
        try:
            _sync_status["running"] = False
            _sync_status["source"] = ""
            _sync_status["started_at"] = None
            _sync_status["completed_at"] = time.time()
            if results is not None:
                total_fetched = sum(_safe_int(o, "pages_fetched") for o in results.values())
                total_chunks = sum(_safe_int(o, "chunks_indexed") for o in results.values())
                total_skipped = sum(_safe_int(o, "pages_skipped") for o in results.values())
                total_failed = sum(_safe_int(o, "pages_failed") + _safe_int(o, "pages_soft_failed") for o in results.values())
                total_shell_suspected = sum(_safe_int(o, "shell_suspected_count") for o in results.values())
                total_js_rendered = sum(_safe_int(o, "pages_js_rendered") for o in results.values())
                any_failed = any(_safe_str(o, "status") == "failed" for o in results.values())
                errors = [_safe_str(o, "error") for o in results.values() if _safe_str(o, "error")]
                _sync_status["last_completed_summary"] = {
                    "source": f"All Active Sources ({len(sources)})",
                    "status": "failed" if any_failed else "ok",
                    "pages_fetched": total_fetched,
                    "chunks_indexed": total_chunks,
                    "pages_skipped": total_skipped,
                    "pages_failed": total_failed,
                    "shell_suspected_count": total_shell_suspected,
                    "pages_js_rendered": total_js_rendered,
                    "error": "; ".join(errors) if errors else None,
                    "finished_at": time.time(),
                }
            else:
                _sync_status["last_completed_summary"] = {
                    "source": f"All Active Sources ({len(sources)})",
                    "status": "failed",
                    "pages_fetched": _sync_status.get("pages_fetched", 0),
                    "chunks_indexed": _sync_status.get("chunks_indexed", 0),
                    "pages_skipped": _sync_status.get("pages_skipped", 0),
                    "pages_failed": _sync_status.get("pages_failed", 0) + 1,
                    "shell_suspected_count": _sync_status.get("shell_suspected_count", 0),
                    "pages_js_rendered": _sync_status.get("pages_js_rendered", 0),
                    "error": exc_message or _sync_status.get("message", "Full sync failed unexpectedly"),
                    "finished_at": time.time(),
                }
            _sync_status["message"] = ""
        finally:
            release_sync_lock()


# --- Helpers -------------------------------------------------------------------------------


def _split_prefixes(raw: str) -> list[str]:
    """Form textareas hold one prefix per line (blank lines/whitespace
    ignored); commas are also accepted as a separator for a single-line
    paste."""
    parts: list[str] = []
    for line in raw.replace(",", "\n").splitlines():
        stripped = line.strip()
        if stripped:
            parts.append(stripped)
    return parts


def _join_prefixes(prefixes: list[str]) -> str:
    return "\n".join(prefixes)


def sitemap_host_differs(sitemap: str | None, base_url: str) -> bool:
    """True when a proposed `sitemap` URL's host differs from `base_url`'s
    host. Used to flag the H1 attack shape in the pending-review table: an
    agent proposing a plausible public `base_url` alongside a `sitemap` that
    actually points at an internal/unrelated host (e.g. a container-network
    address), which is exactly the field the crawler fetches from. A sibling
    task adds server-side rejection for this case; this is the UI half —
    defense in depth, and a useful signal if the rules ever diverge (e.g. a
    legitimately different sitemap host that a future policy allows)."""
    if not sitemap:
        return False
    sitemap_host = urlparse(sitemap).netloc.lower()
    base_host = urlparse(base_url).netloc.lower()
    return bool(sitemap_host) and sitemap_host != base_host


templates.env.globals["sitemap_host_differs"] = sitemap_host_differs
templates.env.globals["supported_fts_languages"] = sorted(SUPPORTED_FTS_LANGUAGES)


def _build_source_config(
    *,
    name: str,
    base_url: str,
    sitemap: str,
    include_prefixes: str,
    exclude_prefixes: str,
    max_pages: str,
    language: str,
    rate_limit_rps: str,
    llms_txt: str = "auto",
    js_render: bool = False,
    source_type: str = "crawl",
    taken: Collection[str] | None = None,
) -> tuple[SourceConfig | None, str | None]:
    """Validate raw form strings into a `SourceConfig`. Returns
    `(cfg, None)` on success or `(None, error_message)` on failure — NEVER
    raises, so callers can always re-render the form with a visible error
    instead of a 500.

    `source_type='upload'` synthesizes `base_url` as the fixed
    `'upload://{name}'` sentinel here, ignoring whatever the form actually
    submitted for `base_url` (an upload source has nothing to crawl, so
    there is no URL to validate) — the submitted `base_url` string never
    reaches `SourceConfig` for an upload source. `SourceConfig`'s own
    `_base_url_matches_source_type` model validator is the real
    enforcement point for the sentinel's exact shape; this just guarantees
    the value handed to it is already correct.

    `taken` is only meaningful on the CREATE path (`create_source_submit`):
    when supplied (not `None`), a blank `include_prefixes`/`max_pages` is
    filled in via `apply_creation_defaults` (derived include-prefix
    scoping and `DEFAULT_MAX_PAGES` as a real ceiling) BEFORE the value
    ever reaches `SourceConfig`. `name` itself is always required and
    explicitly supplied by the caller now (the create form's `name` field
    is required, see bbd4255), so `apply_creation_defaults` never derives
    it — only include_prefixes/max_pages can still be blank-filled. An
    explicitly-supplied value always wins over a derived one. `taken=None`
    (the update path, where a blank name is never valid — the name is
    immutable there) preserves the pre-existing blank-means-"whole
    host"/"unlimited" behavior untouched."""
    try:
        if source_type == "upload":
            base_url = f"upload://{name.strip()}"
        rate_limit_rps_value = rate_limit_rps.strip() or "1.0"
        if taken is not None:
            fields: dict[str, Any] = {"base_url": base_url.strip()}
            name_stripped = name.strip()
            if name_stripped:
                fields["name"] = name_stripped
            include_prefixes_stripped = include_prefixes.strip()
            if include_prefixes_stripped:
                fields["include_prefixes"] = _split_prefixes(include_prefixes)
            max_pages_stripped = max_pages.strip()
            if max_pages_stripped:
                fields["max_pages"] = max_pages_stripped
            fields = apply_creation_defaults(fields, taken)
            name_value = fields["name"]
            include_prefixes_value = fields["include_prefixes"]
            max_pages_value = fields["max_pages"]
        else:
            name_value = name.strip()
            include_prefixes_value = _split_prefixes(include_prefixes)
            max_pages_value = max_pages.strip() or None

        cfg = SourceConfig(
            name=name_value,
            source_type=source_type,
            base_url=base_url.strip(),
            sitemap=sitemap.strip() or None,
            include_prefixes=include_prefixes_value,
            exclude_prefixes=_split_prefixes(exclude_prefixes),
            max_pages=max_pages_value,
            language=language.strip() or "english",
            rate_limit_rps=rate_limit_rps_value,
            llms_txt=(llms_txt.strip() or "auto"),
            js_render=js_render,
        )
        return cfg, None
    except ValidationError as e:
        return None, "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
    except (ValueError, ConfigError) as e:
        return None, str(e)


def _record_to_config(record: SourceRecord) -> SourceConfig:
    """`SourceRecord` -> `SourceConfig` for the fields a manual sync needs.
    Pure, no DB. The `SourceConfig`-shaped columns are re-validated here
    (not merely trusted) because they last passed validation at whatever
    time they were written; re-validating on the sync path is cheap and
    catches a hand-edited-in-SQL row before it reaches the crawler.

    `source_type` MUST be threaded through from `record` here: omitting it
    silently defaulted to `'crawl'`, so an upload-type record's
    `base_url='upload://{name}'` sentinel got validated against the crawl
    URL-scheme rule and raised `pydantic.ValidationError` before any of the
    upload-aware guards in the callers below ever ran (see T19_FIX). Every
    crawl entrypoint below now checks `record.source_type == 'upload'` and
    refuses BEFORE calling this function, so this is defense in depth, not
    the primary guard -- but it also means a caller that forgets to check
    first fails with a clean, correctly-typed `SourceConfig` (which
    `store.sync_source` itself still refuses to crawl) rather than a
    validation crash.
    """
    return SourceConfig(
        name=record.name,
        source_type=record.source_type,
        base_url=record.base_url,
        sitemap=record.sitemap,
        include_prefixes=record.include_prefixes,
        exclude_prefixes=record.exclude_prefixes,
        max_pages=record.max_pages,
        language=record.language or "english",
        rate_limit_rps=record.rate_limit_rps if (record.rate_limit_rps is not None and record.rate_limit_rps > 0) else 1.0,
        llms_txt=record.llms_txt or "auto",
        js_render=record.js_render,
    )


# Severity values a `?level=` query param is allowed to select. WHITELIST, not
# a sanitizer: `_message_level` maps anything outside this set to None, so only
# a literal string from this tuple can ever reach the banner's `class` attribute
# (see `_message_level`).
_ALLOWED_MESSAGE_LEVELS = ("warning",)


def _message_level(request: Request) -> str | None:
    """Severity for the redirect-carried `?msg=` banner, read from the
    companion `?level=` param.

    Severity is carried EXPLICITLY by the redirecting route rather than
    inferred from the message text: string-matching words like "failed"
    would couple presentation to wording and silently mis-colour a banner
    the day someone rephrases a message.

    The return value is interpolated into a CSS class attribute by
    `admin/form.html`/`admin/index.html`, so it is WHITELISTED rather than
    passed through: only an exact match against `_ALLOWED_MESSAGE_LEVELS`
    survives. Absent, empty, mis-cased, or attacker-supplied values (this
    param is fully URL-controlled — anyone can hand an operator a link with
    any `level=` they like) all collapse to None, i.e. the default success
    styling. Autoescaping already blocks the obvious injection, but an
    unvalidated value would still let a caller name any class in the
    stylesheet."""
    level = request.query_params.get("level")
    return level if level in _ALLOWED_MESSAGE_LEVELS else None


# `store.SourceOutcome.status` is one of "ok" | "partial" | "failed" (assigned
# by `store.classify_sync`). Only "ok" is a clean success: "partial" means at
# least one doc in the batch failed to index, "failed" means nothing indexed at
# all — both are outcomes an operator has to act on, so neither may render in a
# green success banner.
_SUCCESS_SYNC_STATUSES = frozenset({"ok"})


def _level_suffix(status: str) -> str:
    """`&level=warning` for a redirect reporting a non-success
    `SourceOutcome.status`, `""` (no param, i.e. success styling) otherwise.

    An unrecognised status — a value a future `classify_sync` might add —
    falls on the warning side deliberately: colouring an unknown outcome
    amber is a cosmetic over-warning, colouring it green would assert a
    success this function cannot actually vouch for."""
    return "" if status in _SUCCESS_SYNC_STATUSES else "&level=warning"


def _form_context(
    request: Request,
    *,
    record: SourceRecord | None = None,
    error: str | None = None,
    values: dict | None = None,
) -> dict:
    """Template context shared by every `admin/form.html` render (the create
    form, the edit form, and both of their validation re-renders).

    `message` is the redirect-carried `?msg=` banner, read the same way
    `list_sources_view` reads it for `admin/index.html` — the routes that
    redirect an operator *to a source page* (`upload_source_submit`'s two
    success redirects, and every upload-aware exit of
    `create_source_submit`) put their outcome there, and without this key
    `form.html` would silently drop it. `.get` returns None when the param
    is absent (every GET of the form, every validation re-render), which is
    falsy, so the banner simply does not render. `error` and `message` are
    independent: a re-render may legitimately carry both, and `form.html`
    renders the error first.

    `message_level` is that banner's severity, from the companion `?level=`
    param (see `_message_level` for the whitelist that guards it). None —
    the case for every success redirect, which deliberately sends no
    `level` at all — renders the default green success banner."""
    values = values or {}
    return {
        "request": request,
        "csrf_token": _expected_csrf_token(),
        "record": record,
        "error": error,
        "values": values,
        "message": request.query_params.get("msg"),
        "message_level": _message_level(request),
    }


# --- Login (unauthenticated) --------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "admin/login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, token: str = Form(...)):
    expected = _sync_token()
    if not expected or not hmac.compare_digest(token, expected):
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {"request": request, "error": "invalid token"},
            status_code=401,
        )
    resp = RedirectResponse(url="/admin", status_code=303)
    resp.set_cookie(
        SESSION_COOKIE,
        _new_session_value(),
        httponly=True,
        secure=True,
        samesite="lax",
        path="/admin",
        max_age=SESSION_MAX_AGE_SECONDS,
    )
    return resp


# --- List ----------------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def list_sources_view(request: Request, _auth=Depends(require_session), conn=Depends(get_conn)):
    active = sources_repo.list_sources(conn, status="active")
    pending = sources_repo.list_sources(conn, status="pending")
    rejected = sources_repo.list_sources(conn, status="rejected")
    return templates.TemplateResponse(
        request,
        "admin/index.html",
        {
            "request": request,
            "active": active,
            "pending": pending,
            "rejected": rejected,
            "csrf_token": _expected_csrf_token(),
            "message": request.query_params.get("msg"),
            # Same severity contract as `_form_context`, so a `?level=` on an
            # /admin redirect colours the banner identically on both pages.
            "message_level": _message_level(request),
            "sync_status": _sync_status,
        },
    )


# --- Create ----------------------------------------------------------------------------------


@router.get("/sources/new", response_class=HTMLResponse)
def new_source_form(request: Request, _auth=Depends(require_session)):
    return templates.TemplateResponse(request, "admin/form.html", _form_context(request))


@router.post("/sources/new", response_class=HTMLResponse)
def create_source_submit(
    request: Request,
    name: str = Form(...),
    # Not `Form(...)`: an upload-type source has no URL to submit at all
    # (see `_build_source_config`, which synthesizes the sentinel
    # 'upload://{name}' internally for source_type='upload' and never reads
    # this raw value in that case). A crawl-type source with a genuinely
    # missing base_url still gets rejected — just via SourceConfig's
    # http(s)-format validation (400) instead of FastAPI's required-field
    # check (422), since the field is no longer required at the Form layer.
    base_url: str = Form(default=""),
    sitemap: str = Form(default=""),
    include_prefixes: str = Form(default=""),
    exclude_prefixes: str = Form(default=""),
    max_pages: str = Form(default=""),
    language: str = Form(default="english"),
    rate_limit_rps: str = Form(default="1.0"),
    llms_txt: str = Form(default="auto"),
    js_render: str = Form(default=""),
    # Defaults to "crawl" if missing/empty for backward safety (e.g. a
    # stale cached form or a direct API call) — the create form itself
    # always submits an explicit value via its source-type radio group.
    source_type: str = Form(default="crawl"),
    # OPTIONAL (`default=[]`, not `File(...)`): the create form is multipart
    # and carries a file input, but attaching a file is only meaningful for
    # source_type='upload'. A crawl-type create submits no usable file part
    # at all — either none (a non-browser/API caller, or a stale cached
    # copy of the pre-upload form) or the empty part a browser sends for an
    # untouched `<input type="file">`. Neither ever reaches a parser: a
    # crawl create returns at the `source_type != "upload"` redirect below
    # (which ignores `files` outright), and an upload create whose parts
    # are all blank returns at the `any(...)` pre-check after it — so
    # `_parse_upload_files`' own skip of blank-filename parts is defense in
    # depth on this route, not the thing that handles them.
    # Making this required would turn every crawl create into a 422.
    files: list[UploadFile] = File(default=[]),
    _auth=Depends(require_csrf),
    conn=Depends(get_conn),
):
    source_type = source_type.strip().lower() or "crawl"
    submitted = {
        "name": name,
        "base_url": base_url,
        "sitemap": sitemap,
        "include_prefixes": include_prefixes,
        "exclude_prefixes": exclude_prefixes,
        "max_pages": max_pages,
        "language": language,
        "rate_limit_rps": rate_limit_rps,
        "llms_txt": llms_txt,
        "js_render": bool(js_render),
    }
    taken: Collection[str] = set()
    cfg, error = _build_source_config(
        name=name,
        base_url=base_url,
        sitemap=sitemap,
        include_prefixes=include_prefixes,
        exclude_prefixes=exclude_prefixes,
        max_pages=max_pages,
        language=language,
        rate_limit_rps=rate_limit_rps,
        llms_txt=llms_txt,
        js_render=bool(js_render),
        source_type=source_type,
        taken=taken,
    )
    if cfg is None:
        return templates.TemplateResponse(
            request,
            "admin/form.html",
            _form_context(request, error=error, values=submitted),
            status_code=400,
        )
    try:
        source_id = sources_repo.create_source(conn, cfg, status="active", proposed_by=None)
    except psycopg.errors.UniqueViolation:
        # A duplicate name is far more likely now that name is auto-derived
        # from base_url — re-render with a 400 and a readable error instead
        # of letting an unhandled 500 poison the request's DB transaction.
        if hasattr(conn, "rollback"):
            try:
                conn.rollback()
            except Exception:
                pass
        logger.warning("admin_source_create_duplicate_name", name=cfg.name)
        return templates.TemplateResponse(
            request,
            "admin/form.html",
            _form_context(
                request,
                error=f"a source named {cfg.name!r} already exists — choose a different name.",
                values=submitted,
            ),
            status_code=400,
        )
    logger.info("admin_source_created", source_id=source_id, name=cfg.name)
    if cfg.source_type != "upload":
        # Crawl-type create: byte-identical to the pre-upload behavior. Any
        # file part that somehow rode along is ignored outright — there is
        # nothing to ingest into a source that gets its content by crawling.
        return RedirectResponse(url=f"/admin?msg=created+{cfg.name}", status_code=303)

    # --- source_type='upload': create + populate in one submit ---------------
    #
    # INVARIANT for everything below: the row created above SURVIVES every
    # downstream failure. Nothing here deletes it or rolls back the create —
    # a failed/partial upload only changes which message the operator lands
    # on, and they retry from the source's own upload form. Correspondingly,
    # no path below may 500: a bad file is operator input, not a server bug.
    if not any((upload.filename or "").strip() for upload in files):
        # Nothing attached (the common "create the source now, upload later"
        # case, and every non-browser caller that posts no file part).
        return RedirectResponse(
            url=f"/admin/sources/{source_id}?msg=created+{quote_plus(cfg.name)}",
            status_code=303,
        )

    # Lock ownership transfers to the background thread once handed off via
    # `run_sync_task` (which releases it inside `_bg_ingest_upload`); every
    # OTHER path out of the block below must release it itself — hence the
    # `handed_off` gate in the `finally`, mirroring `upload_source_submit`.
    # `acquired` additionally gates it because, unlike that route, the lock
    # is taken INSIDE the try here (parsing runs first, so an unparsable
    # batch never contends for the lock at all) — releasing a lock this
    # request never acquired would stomp on whoever does hold it.
    handed_off = False
    acquired = False
    try:
        docs, upload_errors = _parse_upload_files(files)
        if not docs:
            detail = upload_errors[0] if upload_errors else "No parsable content found in the uploaded file(s)."
            logger.warning("admin_create_upload_unparsable", source_id=source_id, name=cfg.name, error=detail)
            return _created_upload_failed_redirect(source_id, cfg.name, detail)

        record = sources_repo.get_source(conn, source_id)
        if record is None:
            # `create_source` just returned this id, so this is a
            # can't-happen (a concurrent delete, or a stubbed repo) — treat
            # it as an upload failure rather than crashing the request.
            logger.error("admin_create_upload_source_missing", source_id=source_id, name=cfg.name)
            return _created_upload_failed_redirect(source_id, cfg.name, "source could not be re-read after creation")

        acquired = try_acquire_sync_lock()
        if not acquired:
            logger.warning("admin_create_upload_lock_busy", source_id=source_id, name=cfg.name)
            # `level=warning`: the source row exists but is EMPTY — the
            # operator must come back and upload. Amber, not green.
            return RedirectResponse(
                url=(
                    f"/admin/sources/{source_id}?msg=created+{quote_plus(cfg.name)}"
                    "+—+upload+skipped:+sync+in+progress&level=warning"
                ),
                status_code=303,
            )

        if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("SYNC_RUNNER_SYNC") == "1":
            outcome = store.ingest_uploaded_docs(conn, record, docs, progress_cb=_on_sync_progress)
            logger.info("admin_create_upload_ingest_complete", source_id=source_id, name=record.name, status=outcome.status)
            # Same self-contradiction guard as `upload_source_submit`'s
            # inline redirect: this message ends in the raw outcome status,
            # so a "partial"/"failed" batch must not land in a green banner
            # just because the CREATE half of the submit succeeded.
            return RedirectResponse(
                url=(
                    f"/admin/sources/{source_id}?msg=created+{quote_plus(cfg.name)}"
                    f":+{quote_plus(str(outcome.status))}{_level_suffix(str(outcome.status))}"
                ),
                status_code=303,
                headers={"HX-Trigger": "syncStatusUpdated"},
            )

        # Hand-off is only real once `run_sync_task` RETURNS: it spawns the
        # thread that owns (and releases) the lock, so if `Thread.start()`
        # raises, the lock is still ours and `handed_off` must still be
        # False for the `finally` to release it.
        run_sync_task(_bg_ingest_upload, record, docs, lambda: conn, source_id)
        handed_off = True
        return RedirectResponse(
            url=f"/admin/sources/{source_id}?msg=upload_started+{quote_plus(cfg.name)}",
            status_code=303,
            headers={"HX-Trigger": "syncStatusUpdated"},
        )
    except Exception as exc:
        # Unlike `upload_source_submit` (where a parser bug may propagate to
        # a 500 because no row was written), the row here is already
        # committed: surfacing a traceback would strand the operator on an
        # error page with no link to the source that DOES now exist. Report
        # it and send them to that source instead.
        # `exc_info=True`: this arm is the catch-all for a genuine parser or
        # repo bug, and the redirect below only carries `str(exc)` — without
        # a traceback in the log there is nothing else to diagnose it from.
        logger.error("admin_create_upload_failed", source_id=source_id, name=cfg.name, error=str(exc), exc_info=True)
        return _created_upload_failed_redirect(source_id, cfg.name, str(exc))
    finally:
        if acquired and not handed_off:
            release_sync_lock()


@router.post("/sources/sync-target", response_class=HTMLResponse)
def sync_target_submit(
    request: Request,
    source_id: int = Form(...),
    _auth=Depends(require_csrf),
    conn=Depends(get_conn),
):
    return sync_source_submit(source_id=source_id, request=request, _auth=_auth, conn=conn)


# --- Edit / update ----------------------------------------------------------------------------


@router.get("/sources/{source_id}", response_class=HTMLResponse)
def edit_source_form(source_id: int, request: Request, _auth=Depends(require_session), conn=Depends(get_conn)):
    record = sources_repo.get_source(conn, source_id)
    if record is None:
        raise HTTPException(status_code=404, detail="source not found")
    values = {
        "name": record.name,
        "base_url": record.base_url,
        "sitemap": record.sitemap or "",
        "include_prefixes": _join_prefixes(record.include_prefixes),
        "exclude_prefixes": _join_prefixes(record.exclude_prefixes),
        "max_pages": str(record.max_pages) if record.max_pages is not None else "",
        "language": record.language,
        "rate_limit_rps": str(record.rate_limit_rps),
        "llms_txt": record.llms_txt or "auto",
        "js_render": record.js_render,
        "schedule_cron": record.schedule_cron or "",
        "enabled": record.enabled,
    }
    return templates.TemplateResponse(request, "admin/form.html", _form_context(request, record=record, values=values))


@router.post("/sources/{source_id}", response_class=HTMLResponse)
def update_source_submit(
    source_id: int,
    request: Request,
    base_url: str = Form(...),
    sitemap: str = Form(default=""),
    include_prefixes: str = Form(default=""),
    exclude_prefixes: str = Form(default=""),
    max_pages: str = Form(default=""),
    language: str = Form(default="english"),
    rate_limit_rps: str = Form(default="1.0"),
    llms_txt: str = Form(default="auto"),
    js_render: str = Form(default=""),
    schedule_cron: str = Form(default=""),
    enabled: str = Form(default=""),
    _auth=Depends(require_csrf),
    conn=Depends(get_conn),
):
    record = sources_repo.get_source(conn, source_id)
    if record is None:
        raise HTTPException(status_code=404, detail="source not found")

    submitted = {
        "name": record.name,
        "base_url": base_url,
        "sitemap": sitemap,
        "include_prefixes": include_prefixes,
        "exclude_prefixes": exclude_prefixes,
        "max_pages": max_pages,
        "language": language,
        "rate_limit_rps": rate_limit_rps,
        "llms_txt": llms_txt,
        "js_render": bool(js_render),
        "schedule_cron": schedule_cron,
        "enabled": bool(enabled),
    }

    # `update_source` requires `name` on `SourceConfig` but never writes it
    # (see sources_repo module docstring) — reuse the existing, immutable
    # name so validation runs against the real record identity. `source_type`
    # is likewise immutable after creation (no selector on the edit form —
    # see form.html) so it is read from the stored record, never from the
    # submitted form.
    cfg, error = _build_source_config(
        name=record.name,
        base_url=base_url,
        sitemap=sitemap,
        include_prefixes=include_prefixes,
        exclude_prefixes=exclude_prefixes,
        max_pages=max_pages,
        language=language,
        rate_limit_rps=rate_limit_rps,
        llms_txt=llms_txt,
        js_render=bool(js_render),
        source_type=record.source_type,
    )
    if cfg is None:
        return templates.TemplateResponse(
            request,
            "admin/form.html",
            _form_context(request, record=record, error=error, values=submitted),
            status_code=400,
        )

    cron_value = schedule_cron.strip() or None
    if cron_value is not None:
        try:
            sources_repo.validate_cron(cron_value)
        except ValueError as e:
            return templates.TemplateResponse(
                request,
                "admin/form.html",
                _form_context(
                    request,
                    record=record,
                    error=(
                        f"invalid schedule: {e} — supported syntax: '*', '*/N', a bare "
                        "integer, or a comma-separated list of integers, in exactly 5 "
                        "space-separated fields (minute hour day month weekday); no "
                        "ranges ('1-5') and no named values ('MON'/'JAN')"
                    ),
                    values=submitted,
                ),
                status_code=400,
            )

    # Everything validated — now, and only now, write. Config first, then
    # the two lifecycle mutators `update_source` deliberately doesn't touch.
    sources_repo.update_source(conn, source_id, cfg)
    sources_repo.set_schedule(conn, source_id, cron_value)
    sources_repo.set_enabled(conn, source_id, bool(enabled))
    logger.info("admin_source_updated", source_id=source_id, name=cfg.name)
    return RedirectResponse(url=f"/admin?msg=updated+{cfg.name}", status_code=303)


# --- Delete --------------------------------------------------------------------------------


@router.post("/sources/{source_id}/delete", response_class=HTMLResponse)
def delete_source_submit(
    source_id: int,
    request: Request,
    _auth=Depends(require_csrf),
    conn=Depends(get_conn),
):
    record = sources_repo.get_source(conn, source_id)
    if record is None:
        raise HTTPException(status_code=404, detail="source not found")
    sources_repo.delete_source(conn, source_id)
    logger.info("admin_source_deleted", source_id=source_id, name=record.name)
    return RedirectResponse(url=f"/admin?msg=deleted+{record.name}", status_code=303)


# --- Manual sync -----------------------------------------------------------------------------


@router.post("/sources/{source_id}/sync", response_class=HTMLResponse)
def sync_source_submit(
    source_id: int,
    request: Request,
    _auth=Depends(require_csrf),
    conn=Depends(get_conn),
):
    record = sources_repo.get_source(conn, source_id)
    if record is None:
        raise HTTPException(status_code=404, detail="source not found")
    if record.status != "active":
        return templates.TemplateResponse(
            request,
            "admin/message.html",
            {
                "request": request,
                "heading": "Cannot sync",
                "message": f"source {record.name!r} is {record.status}, not active — approve it first.",
            },
            status_code=409,
        )
    # T19_FIX: this MUST be checked before `_record_to_config`/lock
    # acquisition below -- an upload-type source's `base_url` is the
    # `upload://{name}` sentinel, which is not a crawlable URL at all
    # ("cannot sync" is correct, not a pending-crawl-config bug). Checking
    # here (before `try_acquire_sync_lock()`) means the lock is never even
    # acquired for this rejection, so there is nothing to leak.
    if record.source_type == "upload":
        return templates.TemplateResponse(
            request,
            "admin/message.html",
            {
                "request": request,
                "heading": "Cannot sync",
                "message": (
                    f"source {record.name!r} is source_type='upload' — it has no URL to crawl; "
                    "use the upload form on its edit page instead."
                ),
            },
            status_code=409,
        )

    acquired = try_acquire_sync_lock()
    if not acquired:
        return templates.TemplateResponse(
            request,
            "admin/message.html",
            {
                "request": request,
                "heading": "Sync already running",
                "message": "another sync is already in progress; try again shortly.",
            },
            status_code=409,
        )

    cfg = _record_to_config(record)
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("SYNC_RUNNER_SYNC") == "1":
        _sync_cancel_event.clear()
        _sync_status["running"] = True
        _sync_status["source"] = cfg.name
        _sync_status["started_at"] = time.time()
        _sync_status["completed_at"] = None
        _sync_status["pages_fetched"] = 0
        _sync_status["chunks_indexed"] = 0
        _sync_status["pages_skipped"] = 0
        _sync_status["pages_failed"] = 0
        _sync_status["shell_suspected_count"] = 0
        _sync_status["pages_js_rendered"] = 0
        _sync_status["last_url"] = ""
        outcome = None
        try:
            outcome = store.sync_source_with_metrics(cfg, conn, progress_cb=_on_sync_progress, cancel_event=_sync_cancel_event)
        finally:
            try:
                _sync_status["running"] = False
                _sync_status["source"] = ""
                _sync_status["started_at"] = None
                _sync_status["completed_at"] = time.time()
                if outcome is not None:
                    status_str = _safe_str(outcome, "status") or "ok"
                    _sync_status["last_completed_summary"] = {
                        "source": cfg.name,
                        "status": status_str,
                        "pages_fetched": _safe_int(outcome, "pages_fetched"),
                        "chunks_indexed": _safe_int(outcome, "chunks_indexed"),
                        "pages_skipped": _safe_int(outcome, "pages_skipped"),
                        "pages_failed": _safe_int(outcome, "pages_failed") + _safe_int(outcome, "pages_soft_failed"),
                        "shell_suspected_count": _safe_int(outcome, "shell_suspected_count"),
                        "pages_js_rendered": _safe_int(outcome, "pages_js_rendered"),
                        "error": _safe_str(outcome, "error"),
                        "finished_at": time.time(),
                    }
            finally:
                release_sync_lock()
        logger.info("admin_manual_sync_complete", source_id=source_id, name=record.name, status=outcome.status)
        return RedirectResponse(
            url=f"/admin?msg=synced+{record.name}:+{outcome.status}",
            status_code=303,
            headers={"HX-Trigger": "syncStatusUpdated"},
        )

    run_sync_task(_bg_sync_single, cfg, lambda: conn, source_id)
    return RedirectResponse(
        url=f"/admin?msg=sync_started+{record.name}",
        status_code=303,
        headers={"HX-Trigger": "syncStatusUpdated"},
    )


@router.post("/sources/{source_id}/purge", response_class=HTMLResponse)
def purge_source_submit(
    source_id: int,
    request: Request,
    _auth=Depends(require_csrf),
    conn=Depends(get_conn),
):
    record = sources_repo.get_source(conn, source_id)
    if record is None:
        raise HTTPException(status_code=404, detail="source not found")

    acquired = try_acquire_sync_lock()
    if not acquired:
        return templates.TemplateResponse(
            request,
            "admin/message.html",
            {
                "request": request,
                "heading": "Sync or Purge already running",
                "message": "another sync/purge is already in progress; try again shortly.",
            },
            status_code=409,
        )

    try:
        count = store.purge_source(conn, source_id)
        if hasattr(conn, "commit"):
            conn.commit()
    except Exception as e:
        logger.error("admin_manual_purge_failed", source_id=source_id, name=record.name, error=str(e))
        return templates.TemplateResponse(
            request,
            "admin/message.html",
            {
                "request": request,
                "heading": "Purge failed",
                "message": f"failed to purge source: {e}",
            },
            status_code=500,
        )
    finally:
        release_sync_lock()

    logger.info("admin_manual_purge_complete", source_id=source_id, name=record.name, pages_deleted=count)
    return RedirectResponse(
        url=f"/admin?msg=purged+{record.name}",
        status_code=303,
        headers={"HX-Trigger": "syncStatusUpdated"},
    )


@router.post("/sources/{source_id}/refresh", response_class=HTMLResponse)
def refresh_source_submit(
    source_id: int,
    request: Request,
    _auth=Depends(require_csrf),
    conn=Depends(get_conn),
):
    record = sources_repo.get_source(conn, source_id)
    if record is None:
        raise HTTPException(status_code=404, detail="source not found")
    if record.status != "active":
        return templates.TemplateResponse(
            request,
            "admin/message.html",
            {
                "request": request,
                "heading": "Cannot refresh",
                "message": f"source {record.name!r} is {record.status}, not active — approve it first.",
            },
            status_code=409,
        )
    # T19_FIX: checked before ANY lock acquisition or `store.purge_source`
    # call below. "Refresh" = purge-then-recrawl; an upload-type source has
    # nothing to recrawl (no URL, no `_record_to_config` beyond the
    # sentinel), so purging it here would permanently delete its content
    # with no way to get it back -- there is no crawl standing by to
    # re-populate it, unlike a real crawl source's refresh.
    if record.source_type == "upload":
        return templates.TemplateResponse(
            request,
            "admin/message.html",
            {
                "request": request,
                "heading": "Cannot refresh",
                "message": (
                    f"source {record.name!r} is source_type='upload' — refresh (purge + recrawl) does not "
                    "apply to upload sources, since there is no crawl to re-populate them; "
                    "use Purge if you want to clear it, then re-upload."
                ),
            },
            status_code=409,
        )

    acquired = try_acquire_sync_lock()
    if not acquired:
        return templates.TemplateResponse(
            request,
            "admin/message.html",
            {
                "request": request,
                "heading": "Sync or Purge already running",
                "message": "another sync/purge is already in progress; try again shortly.",
            },
            status_code=409,
        )

    try:
        count = store.purge_source(conn, source_id)
        if hasattr(conn, "commit"):
            conn.commit()
    except Exception as e:
        release_sync_lock()
        logger.error("admin_manual_refresh_purge_failed", source_id=source_id, name=record.name, error=str(e))
        return templates.TemplateResponse(
            request,
            "admin/message.html",
            {
                "request": request,
                "heading": "Refresh failed",
                "message": f"failed to purge source during refresh: {e}",
            },
            status_code=500,
        )

    cfg = _record_to_config(record)
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("SYNC_RUNNER_SYNC") == "1":
        _sync_cancel_event.clear()
        _sync_status["running"] = True
        _sync_status["source"] = cfg.name
        _sync_status["started_at"] = time.time()
        _sync_status["completed_at"] = None
        _sync_status["pages_fetched"] = 0
        _sync_status["chunks_indexed"] = 0
        _sync_status["pages_skipped"] = 0
        _sync_status["pages_failed"] = 0
        _sync_status["shell_suspected_count"] = 0
        _sync_status["pages_js_rendered"] = 0
        _sync_status["last_url"] = ""
        outcome = None
        try:
            outcome = store.sync_source_with_metrics(cfg, conn, progress_cb=_on_sync_progress, cancel_event=_sync_cancel_event)
        finally:
            try:
                _sync_status["running"] = False
                _sync_status["source"] = ""
                _sync_status["started_at"] = None
                _sync_status["completed_at"] = time.time()
                if outcome is not None:
                    status_str = _safe_str(outcome, "status") or "ok"
                    _sync_status["last_completed_summary"] = {
                        "source": cfg.name,
                        "status": status_str,
                        "pages_fetched": _safe_int(outcome, "pages_fetched"),
                        "chunks_indexed": _safe_int(outcome, "chunks_indexed"),
                        "pages_skipped": _safe_int(outcome, "pages_skipped"),
                        "pages_failed": _safe_int(outcome, "pages_failed") + _safe_int(outcome, "pages_soft_failed"),
                        "shell_suspected_count": _safe_int(outcome, "shell_suspected_count"),
                        "pages_js_rendered": _safe_int(outcome, "pages_js_rendered"),
                        "error": _safe_str(outcome, "error"),
                        "finished_at": time.time(),
                    }
            finally:
                release_sync_lock()
        logger.info(
            "admin_manual_refresh_complete",
            source_id=source_id,
            name=record.name,
            status=outcome.status,
            pages_deleted=count,
        )
        return RedirectResponse(
            url=f"/admin?msg=refreshed+{record.name}:+{outcome.status}",
            status_code=303,
            headers={"HX-Trigger": "syncStatusUpdated"},
        )

    run_sync_task(_bg_sync_single, cfg, lambda: conn, source_id)
    return RedirectResponse(
        url=f"/admin?msg=refresh_started+{record.name}",
        status_code=303,
        headers={"HX-Trigger": "syncStatusUpdated"},
    )


# --- Upload (source_type='upload' only) -----------------------------------------------------


def _upload_edit_values(record: SourceRecord) -> dict:
    """Rebuild the same `values` dict `edit_source_form` renders for
    `record`, for re-rendering `admin/form.html` (the edit view) with an
    error after a failed upload — mirrors `edit_source_form`'s dict
    construction exactly so the re-rendered form looks identical to a fresh
    GET of the edit page."""
    return {
        "name": record.name,
        "base_url": record.base_url,
        "sitemap": record.sitemap or "",
        "include_prefixes": _join_prefixes(record.include_prefixes),
        "exclude_prefixes": _join_prefixes(record.exclude_prefixes),
        "max_pages": str(record.max_pages) if record.max_pages is not None else "",
        "language": record.language,
        "rate_limit_rps": str(record.rate_limit_rps),
        "llms_txt": record.llms_txt or "auto",
        "js_render": record.js_render,
        "schedule_cron": record.schedule_cron or "",
        "enabled": record.enabled,
    }


def _parse_upload_files(files: list[UploadFile]) -> tuple[list[UploadedDoc], list[str]]:
    """Read + parse a multipart file batch into UploadedDocs. Returns
    (docs, error_messages).

    Shared by `upload_source_submit` (the edit-page upload form) and
    `create_source_submit` (the create form's optional file input), so both
    surfaces treat an uploaded batch identically.

    Raw bytes are read here via the blocking `upload.file.read()` sync file
    handle FastAPI provides to a `def` route, and handed to parsers that
    return in-memory `UploadedDoc`s — nothing here writes them to disk (the
    ASGI multipart parser may transiently spool a large file part to an OS
    temp file before `upload.file.read()` ever runs —
    `SpooledTemporaryFile`, framework-layer behavior, not something this
    function does; that file is cleaned up automatically at request end).

    `.zip` uploads go through `upload_zip.expand_zip` directly (not the thin
    `uploads.parse_upload` registry wrapper) so per-member failures can be
    surfaced individually; every other supported suffix goes through
    `uploads.parse_upload`.

    A part whose `filename` is empty/whitespace is SKIPPED, not parsed: a
    browser submits exactly such an empty part for an untouched
    `<input type="file">`, which every create-a-crawl-source submit from
    the (multipart) create form produces. Treating that as a parse failure
    would attach a bogus "unsupported file type: ''" error to submits that
    never attached a file at all.

    `UploadError` is swallowed into the returned error list and NEVER
    raised, so callers can always report a per-file problem instead of
    500ing. Any OTHER exception (a genuine parser bug) still propagates —
    callers that hold the sync lock must release it in a `finally`."""
    docs: list[UploadedDoc] = []
    errors: list[str] = []
    for upload in files:
        filename = upload.filename or ""
        if not filename.strip():
            continue
        data = upload.file.read()
        try:
            if filename.lower().endswith(".zip"):
                result = upload_zip.expand_zip(filename, data)
                docs.extend(result.docs)
                errors.extend(f"{failure.member}: {failure.reason}" for failure in result.failures)
            else:
                docs.extend(uploads.parse_upload(filename, data))
        except UploadError as e:
            errors.append(str(e))
    return docs, errors


def _created_upload_failed_redirect(source_id: int, name: str, detail: str) -> RedirectResponse:
    """Redirect for `create_source_submit` when the source row was created
    successfully but the files attached to the same submit could not be
    ingested. The row ALWAYS survives (it is never deleted or rolled back
    here) — the operator lands on its edit page, where the standing upload
    form lets them retry with a corrected file. `detail` is
    percent-encoded: it is parser-controlled text that can contain `&`,
    `#`, or newlines, none of which may leak into the Location header
    unencoded — which is also why `&level=warning` is appended AFTER the
    encoded detail rather than embedded in it.

    `level=warning` is unconditional here: every caller of this helper is
    reporting a source row that exists but holds none of the content the
    operator just attached."""
    return RedirectResponse(
        url=(
            f"/admin/sources/{source_id}?msg=created+{quote_plus(name)}"
            f"+—+upload+failed:+{quote_plus(detail)}&level=warning"
        ),
        status_code=303,
    )


@router.post("/sources/{source_id}/upload", response_class=HTMLResponse)
def upload_source_submit(
    source_id: int,
    request: Request,
    files: list[UploadFile] = File(...),
    _auth=Depends(require_csrf),
    conn=Depends(get_conn),
):
    """Parse uploaded files (Markdown/text, HTML, PDF, zip bundles) and
    index them into an existing `source_type='upload'` source.

    Reading and parsing the multipart batch lives in `_parse_upload_files`
    (shared with `create_source_submit`) — see its docstring for the
    in-memory/no-disk-write, zip-vs-single-file, and `UploadError`-handling
    contract this route depends on.
    """
    record = sources_repo.get_source(conn, source_id)
    if record is None:
        raise HTTPException(status_code=404, detail="source not found")
    if record.source_type != "upload":
        return templates.TemplateResponse(
            request,
            "admin/message.html",
            {
                "request": request,
                "heading": "Cannot upload",
                "message": f"source {record.name!r} is source_type={record.source_type!r}, not 'upload' — uploads only apply to upload-type sources.",
            },
            status_code=409,
        )

    acquired = try_acquire_sync_lock()
    if not acquired:
        return templates.TemplateResponse(
            request,
            "admin/message.html",
            {
                "request": request,
                "heading": "Sync already running",
                "message": "another sync/upload is already in progress; try again shortly.",
            },
            status_code=409,
        )

    # Ownership of the lock transfers to the background thread once handed
    # off via `run_sync_task`; every OTHER return path below (the no-docs
    # 400, the inline-ingest success path, and any exception raised while
    # reading/parsing files) must release it itself before returning or
    # re-raising — hence `handed_off` gating the `finally` below, mirroring
    # `sync_source_submit`'s acquire/hand-off/release pattern.
    handed_off = False
    try:
        docs, errors = _parse_upload_files(files)

        if not docs:
            error_message = errors[0] if errors else "No parsable content found in the uploaded file(s)."
            return templates.TemplateResponse(
                request,
                "admin/form.html",
                _form_context(request, record=record, error=error_message, values=_upload_edit_values(record)),
                status_code=400,
            )

        if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("SYNC_RUNNER_SYNC") == "1":
            outcome = store.ingest_uploaded_docs(conn, record, docs, progress_cb=_on_sync_progress)
            logger.info("admin_upload_ingest_complete", source_id=source_id, name=record.name, status=outcome.status)
            # The status rides in the message text, so the banner must be
            # coloured by it too: "uploaded widget: failed" in green is a
            # message that contradicts itself.
            return RedirectResponse(
                url=(
                    f"/admin/sources/{source_id}?msg=uploaded+{record.name}:+{outcome.status}"
                    f"{_level_suffix(outcome.status)}"
                ),
                status_code=303,
                headers={"HX-Trigger": "syncStatusUpdated"},
            )

        # Set only after `run_sync_task` returns — the spawned thread owns
        # the lock from that point on, but a raising `Thread.start()` would
        # leave it ours, and the `finally` below must still release it.
        run_sync_task(_bg_ingest_upload, record, docs, lambda: conn, source_id)
        handed_off = True
        return RedirectResponse(
            url=f"/admin/sources/{source_id}?msg=upload_started+{record.name}",
            status_code=303,
            headers={"HX-Trigger": "syncStatusUpdated"},
        )
    finally:
        if not handed_off:
            release_sync_lock()


@router.post("/sync-all", response_class=HTMLResponse)
def sync_all_submit(
    request: Request,
    _auth=Depends(require_csrf),
    conn=Depends(get_conn),
):
    active_sources = sources_repo.list_sources(conn, status="active")
    # T19_FIX: upload-type sources have no URL to crawl (`_record_to_config`
    # re-validates `base_url` and refuses to build a crawlable config for
    # one) -- exclude them from the "sync everything" set up front, the same
    # way `store.sync_all`/`sources_repo.due_sources` already do at the
    # store layer. Previously a single upload source anywhere in
    # `active_sources` made `_record_to_config` raise inside the loop below,
    # aborting the ENTIRE full sync (every crawl source included) instead of
    # being skipped cleanly.
    crawl_sources = [s for s in active_sources if s.source_type != "upload"]
    if not crawl_sources:
        return templates.TemplateResponse(
            request,
            "admin/message.html",
            {
                "request": request,
                "heading": "Cannot sync",
                "message": (
                    "no active crawl sources configured to sync "
                    "(upload-type sources are indexed via their own upload form, not sync)."
                ),
            },
            status_code=409,
        )

    acquired = try_acquire_sync_lock()
    if not acquired:
        return templates.TemplateResponse(
            request,
            "admin/message.html",
            {
                "request": request,
                "heading": "Sync already running",
                "message": "another sync is already in progress; try again shortly.",
            },
            status_code=409,
        )

    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("SYNC_RUNNER_SYNC") == "1":
        _sync_status["running"] = True
        _sync_status["source"] = "All Active Sources"
        _sync_status["started_at"] = time.time()
        _sync_status["completed_at"] = None
        _sync_status["pages_fetched"] = 0
        _sync_status["chunks_indexed"] = 0
        _sync_status["pages_skipped"] = 0
        _sync_status["pages_failed"] = 0
        _sync_status["shell_suspected_count"] = 0
        _sync_status["pages_js_rendered"] = 0
        _sync_status["last_url"] = ""
        results: dict[str, store.SourceOutcome] = {}
        try:
            for rec in crawl_sources:
                cfg = _record_to_config(rec)
                results[cfg.name] = store.sync_source_with_metrics(cfg, conn, progress_cb=_on_sync_progress)
        finally:
            try:
                _sync_status["running"] = False
                _sync_status["source"] = ""
                _sync_status["started_at"] = None
                _sync_status["completed_at"] = time.time()
                total_fetched = sum(_safe_int(o, "pages_fetched") for o in results.values())
                total_chunks = sum(_safe_int(o, "chunks_indexed") for o in results.values())
                total_skipped = sum(_safe_int(o, "pages_skipped") for o in results.values())
                total_failed = sum(_safe_int(o, "pages_failed") + _safe_int(o, "pages_soft_failed") for o in results.values())
                total_shell_suspected = sum(_safe_int(o, "shell_suspected_count") for o in results.values())
                total_js_rendered = sum(_safe_int(o, "pages_js_rendered") for o in results.values())
                any_failed = any(_safe_str(o, "status") == "failed" for o in results.values())
                errors = [_safe_str(o, "error") for o in results.values() if _safe_str(o, "error")]
                _sync_status["last_completed_summary"] = {
                    "source": f"All Active Sources ({len(crawl_sources)})",
                    "status": "failed" if any_failed else "ok",
                    "pages_fetched": total_fetched,
                    "chunks_indexed": total_chunks,
                    "pages_skipped": total_skipped,
                    "pages_failed": total_failed,
                    "shell_suspected_count": total_shell_suspected,
                    "pages_js_rendered": total_js_rendered,
                    "error": "; ".join(errors) if errors else None,
                    "finished_at": time.time(),
                }
            finally:
                release_sync_lock()
        logger.info("admin_full_sync_complete", count=len(crawl_sources))
        return RedirectResponse(
            url="/admin?msg=full_sync_completed",
            status_code=303,
            headers={"HX-Trigger": "syncStatusUpdated"},
        )

    run_sync_task(_bg_sync_all, crawl_sources, lambda: conn)
    return RedirectResponse(
        url="/admin?msg=full_sync_started",
        status_code=303,
        headers={"HX-Trigger": "syncStatusUpdated"},
    )


@router.get("/sync-status-widget", response_class=HTMLResponse)
def sync_status_widget_view(request: Request, _auth=Depends(require_session)):
    return templates.TemplateResponse(
        request,
        "admin/_sync_status_partial.html",
        {
            "request": request,
            "sync_status": _sync_status,
            "csrf_token": _expected_csrf_token(),
        },
    )


@router.post("/sync-status/clear", response_class=HTMLResponse)
def clear_sync_status_view(request: Request, _auth=Depends(require_csrf)):
    _sync_status.pop("last_completed_summary", None)
    return templates.TemplateResponse(
        request,
        "admin/_sync_status_partial.html",
        {
            "request": request,
            "sync_status": _sync_status,
            "csrf_token": _expected_csrf_token(),
        },
    )


@router.post("/sync/stop", response_class=HTMLResponse)
def stop_sync_submit(request: Request, _auth=Depends(require_csrf)):
    _sync_cancel_event.set()
    logger.info("admin_manual_stop_triggered")
    return RedirectResponse(
        url="/admin?msg=stop_triggered",
        status_code=303,
        headers={"HX-Trigger": "syncStatusUpdated"},
    )


@router.get("/docs", response_class=HTMLResponse)
def list_docs_view(
    request: Request,
    source_id: int | None = None,
    query: str | None = None,
    _auth=Depends(require_session),
    conn=Depends(get_conn),
):
    pages = store.list_doc_pages(conn, source_id=source_id, query=query, limit=200)
    sources = sources_repo.list_sources(conn, status="active")
    return templates.TemplateResponse(
        request,
        "admin/docs.html",
        {
            "request": request,
            "pages": pages,
            "sources": sources,
            "selected_source_id": source_id,
            "query": query or "",
            "csrf_token": _expected_csrf_token(),
            "sync_status": _sync_status,
        },
    )


@router.get("/docs/pages/{page_id}/chunks", response_class=HTMLResponse)
def get_page_chunks_view(
    page_id: int,
    request: Request,
    _auth=Depends(require_session),
    conn=Depends(get_conn),
):
    chunks = store.get_page_chunks(conn, page_id)
    return templates.TemplateResponse(
        request,
        "admin/_chunks_partial.html",
        {
            "request": request,
            "chunks": chunks,
            "page_id": page_id,
        },
    )


# --- Approve / reject --------------------------------------------------------------------------


@router.post("/sources/{source_id}/approve", response_class=HTMLResponse)
def approve_source_submit(
    source_id: int,
    request: Request,
    _auth=Depends(require_csrf),
    conn=Depends(get_conn),
):
    record = sources_repo.get_source(conn, source_id)
    if record is None:
        raise HTTPException(status_code=404, detail="source not found")
    sources_repo.set_status(conn, source_id, "active")
    logger.info("admin_source_approved", source_id=source_id, name=record.name)
    return RedirectResponse(url=f"/admin?msg=approved+{record.name}", status_code=303)


@router.post("/sources/{source_id}/reject", response_class=HTMLResponse)
def reject_source_submit(
    source_id: int,
    request: Request,
    _auth=Depends(require_csrf),
    conn=Depends(get_conn),
):
    record = sources_repo.get_source(conn, source_id)
    if record is None:
        raise HTTPException(status_code=404, detail="source not found")
    sources_repo.set_status(conn, source_id, "rejected")
    logger.info("admin_source_rejected", source_id=source_id, name=record.name)
    return RedirectResponse(url=f"/admin?msg=rejected+{record.name}", status_code=303)
