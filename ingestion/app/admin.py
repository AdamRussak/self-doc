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
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from . import sources_repo, store
from .config import SUPPORTED_FTS_LANGUAGES, ConfigError, SourceConfig
from .logging_config import get_logger
from .sources_repo import SourceRecord

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
    _sync_status["running"] = True
    _sync_status["source"] = cfg.name
    _sync_status["started_at"] = time.time()
    _sync_status["completed_at"] = None
    _sync_status["message"] = f"Syncing {cfg.name}..."
    _sync_status["pages_fetched"] = 0
    _sync_status["chunks_indexed"] = 0
    _sync_status["pages_skipped"] = 0
    _sync_status["pages_failed"] = 0
    _sync_status["last_url"] = ""
    outcome: store.SourceOutcome | None = None
    exc_message: str | None = None
    try:
        if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("SYNC_RUNNER_SYNC") == "1":
            conn = conn_factory()
            outcome = store.sync_source(cfg, conn, progress_cb=_on_sync_progress)
        else:
            conn = store.get_connection()
            try:
                outcome = store.sync_source(cfg, conn, progress_cb=_on_sync_progress)
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
                    "error": exc_message or _sync_status.get("message", "Sync failed unexpectedly"),
                    "finished_at": time.time(),
                }
            _sync_status["message"] = ""
        finally:
            release_sync_lock()


def _bg_sync_all(sources: list[SourceRecord], conn_factory: Callable[[], Any]) -> None:
    """Worker for background full sync across all active sources."""
    _sync_status["running"] = True
    _sync_status["source"] = "All Active Sources"
    _sync_status["started_at"] = time.time()
    _sync_status["completed_at"] = None
    _sync_status["message"] = f"Full sync started ({len(sources)} sources)..."
    _sync_status["pages_fetched"] = 0
    _sync_status["chunks_indexed"] = 0
    _sync_status["pages_skipped"] = 0
    _sync_status["pages_failed"] = 0
    _sync_status["last_url"] = ""
    results: dict[str, store.SourceOutcome] | None = None
    exc_message: str | None = None
    try:
        if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("SYNC_RUNNER_SYNC") == "1":
            conn = conn_factory()
            results = {}
            for rec in sources:
                cfg = _record_to_config(rec)
                results[cfg.name] = store.sync_source(cfg, conn, progress_cb=_on_sync_progress)
        else:
            cfgs = [_record_to_config(rec) for rec in sources]
            results = store.sync_all(cfgs, progress_cb=_on_sync_progress)
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
                any_failed = any(_safe_str(o, "status") == "failed" for o in results.values())
                errors = [_safe_str(o, "error") for o in results.values() if _safe_str(o, "error")]
                _sync_status["last_completed_summary"] = {
                    "source": f"All Active Sources ({len(sources)})",
                    "status": "failed" if any_failed else "ok",
                    "pages_fetched": total_fetched,
                    "chunks_indexed": total_chunks,
                    "pages_skipped": total_skipped,
                    "pages_failed": total_failed,
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
) -> tuple[SourceConfig | None, str | None]:
    """Validate raw form strings into a `SourceConfig`. Returns
    `(cfg, None)` on success or `(None, error_message)` on failure — NEVER
    raises, so callers can always re-render the form with a visible error
    instead of a 500."""
    try:
        cfg = SourceConfig(
            name=name.strip(),
            base_url=base_url.strip(),
            sitemap=sitemap.strip() or None,
            include_prefixes=_split_prefixes(include_prefixes),
            exclude_prefixes=_split_prefixes(exclude_prefixes),
            max_pages=max_pages.strip(),
            language=language.strip() or "english",
            rate_limit_rps=rate_limit_rps.strip(),
            llms_txt=(llms_txt.strip() or "auto"),
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
    catches a hand-edited-in-SQL row before it reaches the crawler."""
    return SourceConfig(
        name=record.name,
        base_url=record.base_url,
        sitemap=record.sitemap,
        include_prefixes=record.include_prefixes,
        exclude_prefixes=record.exclude_prefixes,
        max_pages=record.max_pages if (record.max_pages is not None and record.max_pages > 0) else 100,
        language=record.language or "english",
        rate_limit_rps=record.rate_limit_rps if (record.rate_limit_rps is not None and record.rate_limit_rps > 0) else 1.0,
        llms_txt=record.llms_txt or "auto",
    )


def _form_context(
    request: Request,
    *,
    record: SourceRecord | None = None,
    error: str | None = None,
    values: dict | None = None,
) -> dict:
    values = values or {}
    return {
        "request": request,
        "csrf_token": _expected_csrf_token(),
        "record": record,
        "error": error,
        "values": values,
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
    base_url: str = Form(...),
    sitemap: str = Form(default=""),
    include_prefixes: str = Form(default=""),
    exclude_prefixes: str = Form(default=""),
    max_pages: str = Form(...),
    language: str = Form(default="english"),
    rate_limit_rps: str = Form(default="1.0"),
    llms_txt: str = Form(default="auto"),
    _auth=Depends(require_csrf),
    conn=Depends(get_conn),
):
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
    }
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
    )
    if cfg is None:
        return templates.TemplateResponse(
            request,
            "admin/form.html",
            _form_context(request, error=error, values=submitted),
            status_code=400,
        )
    source_id = sources_repo.create_source(conn, cfg, status="active", proposed_by=None)
    logger.info("admin_source_created", source_id=source_id, name=cfg.name)
    return RedirectResponse(url=f"/admin?msg=created+{cfg.name}", status_code=303)


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
    max_pages: str = Form(...),
    language: str = Form(default="english"),
    rate_limit_rps: str = Form(default="1.0"),
    llms_txt: str = Form(default="auto"),
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
        "schedule_cron": schedule_cron,
        "enabled": bool(enabled),
    }

    # `update_source` requires `name` on `SourceConfig` but never writes it
    # (see sources_repo module docstring) — reuse the existing, immutable
    # name so validation runs against the real record identity.
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
        _sync_status["running"] = True
        _sync_status["source"] = cfg.name
        _sync_status["started_at"] = time.time()
        _sync_status["completed_at"] = None
        _sync_status["pages_fetched"] = 0
        _sync_status["chunks_indexed"] = 0
        _sync_status["pages_skipped"] = 0
        _sync_status["pages_failed"] = 0
        _sync_status["last_url"] = ""
        outcome = None
        try:
            outcome = store.sync_source(cfg, conn, progress_cb=_on_sync_progress)
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


@router.post("/sync-all", response_class=HTMLResponse)
def sync_all_submit(
    request: Request,
    _auth=Depends(require_csrf),
    conn=Depends(get_conn),
):
    active_sources = sources_repo.list_sources(conn, status="active")
    if not active_sources:
        return templates.TemplateResponse(
            request,
            "admin/message.html",
            {
                "request": request,
                "heading": "Cannot sync",
                "message": "no active sources configured to sync.",
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
        _sync_status["last_url"] = ""
        results: dict[str, store.SourceOutcome] = {}
        try:
            for rec in active_sources:
                cfg = _record_to_config(rec)
                results[cfg.name] = store.sync_source(cfg, conn, progress_cb=_on_sync_progress)
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
                any_failed = any(_safe_str(o, "status") == "failed" for o in results.values())
                errors = [_safe_str(o, "error") for o in results.values() if _safe_str(o, "error")]
                _sync_status["last_completed_summary"] = {
                    "source": f"All Active Sources ({len(active_sources)})",
                    "status": "failed" if any_failed else "ok",
                    "pages_fetched": total_fetched,
                    "chunks_indexed": total_chunks,
                    "pages_skipped": total_skipped,
                    "pages_failed": total_failed,
                    "error": "; ".join(errors) if errors else None,
                    "finished_at": time.time(),
                }
            finally:
                release_sync_lock()
        logger.info("admin_full_sync_complete", count=len(active_sources))
        return RedirectResponse(
            url="/admin?msg=full_sync_completed",
            status_code=303,
            headers={"HX-Trigger": "syncStatusUpdated"},
        )

    run_sync_task(_bg_sync_all, active_sources, lambda: conn)
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
