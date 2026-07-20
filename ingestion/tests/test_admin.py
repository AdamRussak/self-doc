"""Tests for app.admin (the source-management admin UI).

No live Postgres is reachable from this test process (the ingestion
container's db port is deliberately unpublished) — every test here runs
WITHOUT a database: `admin.get_conn` is overridden with a dependency that
yields a sentinel object, and every `sources_repo`/`store` call the routes
make is monkeypatched. This gives real coverage of auth, CSRF, form
validation, and rendering — the router logic this module owns — while
leaving `sources_repo`'s own DB-dependent functions untested here (they're
covered, or explicitly skipped, in `test_sources_repo.py`).

Every test in this file EXECUTES (none are skipped): nothing here touches a
database.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from datetime import datetime, timezone
from app import admin
from app.config import ConfigError, SourceConfig
from app.sources_repo import SourceRecord
from app.store import ChunkRecord, PageRecord, SourceOutcome

SYNC_TOKEN = "test-admin-token-xyz"


@pytest.fixture(autouse=True)
def _sync_token_env(monkeypatch):
    monkeypatch.setenv("SYNC_TOKEN", SYNC_TOKEN)


@pytest.fixture
def app():
    application = FastAPI()
    application.include_router(admin.router)
    # No real DB: get_conn is overridden with a sentinel. Every route also
    # goes through sources_repo/store, which individual tests monkeypatch.
    def fake_get_conn():
        yield object()

    application.dependency_overrides[admin.get_conn] = fake_get_conn
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
def client(app):
    # base_url is https (not the default http://testserver): the session
    # cookie is Secure now (M2 fix), and httpx's cookie jar — correctly —
    # refuses to attach a Secure cookie to a plain-http request, so a
    # plain-http TestClient would silently drop the cookie on every request
    # after login and every "authenticated" test would 401.
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def csrf_token():
    return admin._expected_csrf_token()


def _login(client) -> None:
    resp = client.post("/admin/login", data={"token": SYNC_TOKEN}, follow_redirects=False)
    assert resp.status_code == 303
    assert admin.SESSION_COOKIE in client.cookies


def _make_record(**overrides) -> SourceRecord:
    defaults = dict(
        id=1,
        name="widget",
        base_url="https://widget.example.com/docs/",
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


# --- Login -----------------------------------------------------------------------------


def test_login_form_renders_without_auth(client):
    resp = client.get("/admin/login")
    assert resp.status_code == 200
    assert "SYNC_TOKEN" in resp.text


def test_login_wrong_token_rejected(client):
    resp = client.post("/admin/login", data={"token": "not-the-token"})
    assert resp.status_code == 401
    assert admin.SESSION_COOKIE not in client.cookies


def test_login_correct_token_sets_cookie(client):
    _login(client)
    assert admin.SESSION_COOKIE in client.cookies


def test_login_sets_secure_cookie_with_expiry(client):
    """M2 fix: the session cookie must carry `Secure` and a bounded max-age,
    not just `HttpOnly`+`SameSite`. Inspected via the raw `Set-Cookie`
    header since httpx's high-level `client.cookies` jar does not expose
    cookie attributes."""
    resp = client.post("/admin/login", data={"token": SYNC_TOKEN}, follow_redirects=False)
    assert resp.status_code == 303
    set_cookie = resp.headers["set-cookie"]
    assert "Secure" in set_cookie
    assert "HttpOnly" in set_cookie
    assert f"Max-Age={admin.SESSION_MAX_AGE_SECONDS}" in set_cookie


# --- Session cookie: expiry + tamper-evidence (M2) --------------------------------------


def test_expired_session_cookie_is_rejected(client, monkeypatch):
    """A cookie whose HMAC is valid for its embedded `issued_at` but whose
    age exceeds SESSION_MAX_AGE_SECONDS must be rejected — expiry without a
    server-side session store."""
    stale_issued_at = int(time.time()) - admin.SESSION_MAX_AGE_SECONDS - 60
    client.cookies.set(admin.SESSION_COOKIE, admin._session_value_for(stale_issued_at))
    resp = client.get("/admin")
    assert resp.status_code == 401


def test_fresh_session_cookie_within_max_age_is_accepted(client):
    """Sanity check on the boundary: a cookie issued well within the max-age
    window is accepted."""
    fresh_issued_at = int(time.time()) - 5
    client.cookies.set(admin.SESSION_COOKIE, admin._session_value_for(fresh_issued_at))
    resp = client.get("/admin/sources/new")
    assert resp.status_code == 200


def test_tampered_issued_at_timestamp_is_rejected(client):
    """M2 tamper-evidence: the timestamp is bound INTO the HMAC message, not
    merely appended next to an unrelated digest. Editing `issued_at` in an
    otherwise-valid cookie (without knowing SYNC_TOKEN, so the digest can't
    be recomputed to match) must invalidate the whole cookie — whether the
    edited timestamp is pushed into the future (extend/forge a session) or
    just altered arbitrarily."""
    issued_at = int(time.time())
    valid_value = admin._session_value_for(issued_at)
    _issued_at_str, _, digest = valid_value.partition(".")

    # Forge a future issued_at, keeping the OLD (now-mismatched) digest.
    forged_future = f"{issued_at + 999999}.{digest}"
    client.cookies.set(admin.SESSION_COOKIE, forged_future)
    resp = client.get("/admin")
    assert resp.status_code == 401

    # Forge an arbitrarily-edited (but still well-formed) issued_at.
    forged_edited = f"{issued_at + 1}.{digest}"
    client.cookies.set(admin.SESSION_COOKIE, forged_edited)
    resp = client.get("/admin")
    assert resp.status_code == 401


def test_malformed_session_cookie_is_rejected(client):
    for bogus in ["not-a-valid-cookie-format", "12345", "abc.def", "", "12345."]:
        client.cookies.set(admin.SESSION_COOKIE, bogus)
        resp = client.get("/admin")
        assert resp.status_code == 401, f"bogus cookie {bogus!r} was accepted"


# --- Auth: every route below rejects an unauthenticated request, per-route -------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/admin"),
        ("get", "/admin/sources/new"),
        ("post", "/admin/sources/new"),
        ("get", "/admin/sources/1"),
        ("post", "/admin/sources/1"),
        ("post", "/admin/sources/1/delete"),
        ("post", "/admin/sources/1/sync"),
        ("post", "/admin/sources/1/approve"),
        ("post", "/admin/sources/1/reject"),
    ],
)
def test_route_rejects_unauthenticated(client, method, path):
    resp = getattr(client, method)(path)
    assert resp.status_code == 401, f"{method.upper()} {path} did not reject unauthenticated request"


# --- CSRF: authenticated but missing/wrong csrf_token on a POST is rejected ------------


@pytest.mark.parametrize(
    "path",
    [
        "/admin/sources/new",
        "/admin/sources/1",
        "/admin/sources/1/delete",
        "/admin/sources/1/sync",
        "/admin/sources/1/approve",
        "/admin/sources/1/reject",
    ],
)
def test_post_route_rejects_missing_csrf_token(client, path):
    _login(client)
    resp = client.post(path, data={})
    assert resp.status_code == 403


def test_post_route_rejects_wrong_csrf_token(client):
    _login(client)
    resp = client.post("/admin/sources/1/approve", data={"csrf_token": "not-the-right-value"})
    assert resp.status_code == 403


def test_forged_cross_origin_post_without_csrf_is_rejected(client, monkeypatch):
    """Simulates the concrete CSRF attack this defends against: an attacker
    page cannot know `csrf_token` (it is derived from `SYNC_TOKEN`, which the
    attacker never sees), so a forged POST — even if the browser attached
    the session cookie — omits it and must be rejected."""
    _login(client)
    approve_mock = MagicMock()
    monkeypatch.setattr(admin.sources_repo, "set_status", approve_mock)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=_make_record()))

    forged = client.post("/admin/sources/1/approve", data={})  # no csrf_token: exactly what a forged cross-origin form would send
    assert forged.status_code == 403
    approve_mock.assert_not_called()


# --- List view: pending sources render in a visually distinct, labeled section ---------


def test_index_lists_active_and_labels_pending_by_proposer(client, monkeypatch):
    _login(client)
    active = [_make_record(id=1, name="active-src", status="active")]
    pending = [_make_record(id=2, name="pending-src", status="pending", proposed_by="agent-mcp-tool")]

    def fake_list(conn, *, status=None):
        return {"active": active, "pending": pending, "rejected": []}[status]

    monkeypatch.setattr(admin.sources_repo, "list_sources", fake_list)

    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "pending-src" in resp.text
    assert "active-src" in resp.text
    assert "agent-mcp-tool" in resp.text
    # The pending section is visually distinct (own CSS class) and names the proposer.
    assert "pending-section" in resp.text
    assert "proposed-by" in resp.text


def test_pending_table_renders_sitemap_and_crawl_scope_fields(client, monkeypatch):
    """H1 fix: the pending-review table must render `sitemap`,
    `include_prefixes`, `exclude_prefixes` and `max_pages` — not just the
    (safe-looking) `base_url` — since `sitemap` is the field the crawler
    actually fetches from and an agent can point it anywhere."""
    _login(client)
    pending = [
        _make_record(
            id=2,
            name="pending-src",
            status="pending",
            proposed_by="agent-mcp-tool",
            base_url="https://real-docs.example.com/",
            sitemap="http://192.168.1.1/api/v1/config",
            include_prefixes=["/docs/", "/api/"],
            exclude_prefixes=["/blog/"],
            max_pages=250,
        )
    ]
    monkeypatch.setattr(
        admin.sources_repo,
        "list_sources",
        lambda conn, *, status=None: {"active": [], "pending": pending, "rejected": []}[status],
    )

    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "http://192.168.1.1/api/v1/config" in resp.text
    assert "/docs/" in resp.text
    assert "/api/" in resp.text
    assert "/blog/" in resp.text
    assert "250" in resp.text
    # A sitemap host that differs from base_url's host must be visibly flagged.
    assert 'class="col-sitemap sitemap-mismatch"' in resp.text
    assert "host differs" in resp.text.lower()


def test_pending_table_no_mismatch_warning_when_hosts_match(client, monkeypatch):
    _login(client)
    pending = [
        _make_record(
            id=2,
            name="pending-src",
            status="pending",
            base_url="https://docs.example.com/",
            sitemap="https://docs.example.com/sitemap.xml",
        )
    ]
    monkeypatch.setattr(
        admin.sources_repo,
        "list_sources",
        lambda conn, *, status=None: {"active": [], "pending": pending, "rejected": []}[status],
    )

    resp = client.get("/admin")
    assert resp.status_code == 200
    assert 'class="col-sitemap sitemap-mismatch"' not in resp.text


def test_delete_form_has_confirmation(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(admin.sources_repo, "list_sources", lambda conn, *, status=None: (
        [_make_record()] if status == "active" else []
    ))
    resp = client.get("/admin")
    assert "confirm(" in resp.text


def test_delete_forms_do_not_interpolate_into_a_js_context(client, monkeypatch):
    """L1 fix: no `onsubmit="...confirm('...' + {{ s.name }} + ...)"` inline
    handler anywhere — HTML-escaping (which IS on) does not protect a value
    landing inside a JS string literal inside an HTML attribute. Delete
    confirmation must instead be driven by a delegated listener reading
    `data-*` attributes (safe: HTML-attribute context, not a JS-string
    context)."""
    _login(client)
    record = _make_record(id=9, name="widget", status="active")
    monkeypatch.setattr(
        admin.sources_repo,
        "list_sources",
        lambda conn, *, status=None: {"active": [record], "pending": [], "rejected": [record]}[status],
    )

    resp = client.get("/admin")
    assert resp.status_code == 200
    # `onsubmit=` (an attribute assignment) must be absent from every FORM
    # tag; the word may still legitimately appear inside the base.html
    # explanatory JS comment describing the fix, so check for the attribute
    # syntax specifically rather than the bare substring.
    assert "onsubmit=" not in resp.text
    assert 'data-confirm-delete' in resp.text
    assert 'data-source-name="widget"' in resp.text


def test_no_template_interpolates_a_server_value_into_a_javascript_context():
    """Static grep proof for L1: scan every admin template for any
    `{{ ... }}` Jinja expression that lands inside a `<script>` block or an
    inline JS-attribute (`on*="..."`) — the two JS contexts where HTML
    escaping does not protect against injection. There must be none; all
    dynamic values are rendered only into HTML text/attribute context."""
    import re

    templates_dir = admin.TEMPLATES_DIR
    on_attr_re = re.compile(r'\bon\w+\s*=\s*"[^"]*\{\{')
    for path in templates_dir.rglob("*.html"):
        text = path.read_text()
        assert not on_attr_re.search(text), f"{path}: inline JS-attribute handler interpolates a Jinja expression"
        for script_match in re.finditer(r"<script\b[^>]*>(.*?)</script>", text, re.DOTALL):
            script_body = script_match.group(1)
            assert "{{" not in script_body, f"{path}: <script> block interpolates a Jinja expression"


def test_no_template_makes_an_external_network_request():
    """Grep every admin template for a CDN/external URL — htmx must be
    served from this app's own /admin/static route, never hotlinked."""
    templates_dir = admin.TEMPLATES_DIR
    for path in templates_dir.rglob("*.html"):
        text = path.read_text()
        for line in text.splitlines():
            if line.strip().startswith("<!--"):
                continue  # comments may *mention* "CDN" while explaining why we avoid one
            assert "http://" not in line and "https://" not in line, f"{path}: {line}"


# --- Create ------------------------------------------------------------------------------


def test_create_invalid_input_rerenders_form_and_writes_nothing(client, csrf_token, monkeypatch):
    _login(client)
    create_mock = MagicMock()
    monkeypatch.setattr(admin.sources_repo, "create_source", create_mock)

    resp = client.post(
        "/admin/sources/new",
        data={
            "csrf_token": csrf_token,
            "name": "Not Valid Name!",  # violates NAME_PATTERN
            "base_url": "https://example.com/",
            "max_pages": "10",
        },
    )
    assert resp.status_code == 400
    assert "error" in resp.text.lower()
    create_mock.assert_not_called()


def test_create_bad_url_rerenders_form_and_writes_nothing(client, csrf_token, monkeypatch):
    _login(client)
    create_mock = MagicMock()
    monkeypatch.setattr(admin.sources_repo, "create_source", create_mock)

    resp = client.post(
        "/admin/sources/new",
        data={
            "csrf_token": csrf_token,
            "name": "widget",
            "base_url": "not-a-url",
            "max_pages": "10",
        },
    )
    assert resp.status_code == 400
    create_mock.assert_not_called()


def test_create_valid_input_calls_create_source_and_redirects(client, csrf_token, monkeypatch):
    _login(client)
    create_mock = MagicMock(return_value=42)
    monkeypatch.setattr(admin.sources_repo, "create_source", create_mock)

    resp = client.post(
        "/admin/sources/new",
        data={
            "csrf_token": csrf_token,
            "name": "widget",
            "base_url": "https://widget.example.com/docs/",
            "max_pages": "100",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin")
    create_mock.assert_called_once()
    _conn, cfg = create_mock.call_args.args
    assert isinstance(cfg, SourceConfig)
    assert cfg.name == "widget"
    assert create_mock.call_args.kwargs["status"] == "active"
    assert create_mock.call_args.kwargs["proposed_by"] is None


# --- Edit / update -------------------------------------------------------------------------


def test_edit_form_renders_existing_values(client, monkeypatch):
    _login(client)
    record = _make_record(id=7, name="widget", schedule_cron="0 3 * * *")
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))

    resp = client.get("/admin/sources/7")
    assert resp.status_code == 200
    assert "widget" in resp.text
    assert "0 3 * * *" in resp.text


def test_edit_missing_source_is_404(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=None))
    resp = client.get("/admin/sources/999")
    assert resp.status_code == 404


def test_update_invalid_config_writes_nothing(client, csrf_token, monkeypatch):
    _login(client)
    record = _make_record(id=7)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    update_mock = MagicMock()
    schedule_mock = MagicMock()
    enabled_mock = MagicMock()
    monkeypatch.setattr(admin.sources_repo, "update_source", update_mock)
    monkeypatch.setattr(admin.sources_repo, "set_schedule", schedule_mock)
    monkeypatch.setattr(admin.sources_repo, "set_enabled", enabled_mock)

    resp = client.post(
        "/admin/sources/7",
        data={"csrf_token": csrf_token, "base_url": "not-a-url", "max_pages": "10"},
    )
    assert resp.status_code == 400
    update_mock.assert_not_called()
    schedule_mock.assert_not_called()
    enabled_mock.assert_not_called()


def test_update_invalid_cron_writes_nothing_even_though_config_was_valid(client, csrf_token, monkeypatch):
    """A valid SourceConfig paired with an unsupported cron expression must
    write NEITHER the config NOR the schedule/enabled fields — validation of
    every field happens before any write."""
    _login(client)
    record = _make_record(id=7)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    update_mock = MagicMock()
    schedule_mock = MagicMock()
    enabled_mock = MagicMock()
    monkeypatch.setattr(admin.sources_repo, "update_source", update_mock)
    monkeypatch.setattr(admin.sources_repo, "set_schedule", schedule_mock)
    monkeypatch.setattr(admin.sources_repo, "set_enabled", enabled_mock)

    resp = client.post(
        "/admin/sources/7",
        data={
            "csrf_token": csrf_token,
            "base_url": "https://widget.example.com/docs/",
            "max_pages": "100",
            "schedule_cron": "1-5 * * * *",  # ranges are unsupported
        },
    )
    assert resp.status_code == 400
    assert "supported syntax" in resp.text.lower() or "unsupported" in resp.text.lower()
    update_mock.assert_not_called()
    schedule_mock.assert_not_called()
    enabled_mock.assert_not_called()


def test_update_valid_calls_update_schedule_and_enabled(client, csrf_token, monkeypatch):
    _login(client)
    record = _make_record(id=7, name="widget")
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    update_mock = MagicMock()
    schedule_mock = MagicMock()
    enabled_mock = MagicMock()
    monkeypatch.setattr(admin.sources_repo, "update_source", update_mock)
    monkeypatch.setattr(admin.sources_repo, "set_schedule", schedule_mock)
    monkeypatch.setattr(admin.sources_repo, "set_enabled", enabled_mock)

    resp = client.post(
        "/admin/sources/7",
        data={
            "csrf_token": csrf_token,
            "base_url": "https://widget.example.com/docs/",
            "max_pages": "100",
            "schedule_cron": "0 3 * * *",
            "enabled": "yes",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    update_mock.assert_called_once()
    schedule_mock.assert_called_once_with(update_mock.call_args.args[0], 7, "0 3 * * *")
    enabled_mock.assert_called_once_with(update_mock.call_args.args[0], 7, True)


def test_update_unchecked_enabled_disables_source(client, csrf_token, monkeypatch):
    _login(client)
    record = _make_record(id=7, name="widget")
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    monkeypatch.setattr(admin.sources_repo, "update_source", MagicMock())
    monkeypatch.setattr(admin.sources_repo, "set_schedule", MagicMock())
    enabled_mock = MagicMock()
    monkeypatch.setattr(admin.sources_repo, "set_enabled", enabled_mock)

    resp = client.post(
        "/admin/sources/7",
        data={
            "csrf_token": csrf_token,
            "base_url": "https://widget.example.com/docs/",
            "max_pages": "100",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    enabled_mock.assert_called_once_with(enabled_mock.call_args.args[0], 7, False)


# --- Delete ------------------------------------------------------------------------------


def test_delete_calls_delete_source(client, csrf_token, monkeypatch):
    _login(client)
    record = _make_record(id=9, name="doomed")
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    delete_mock = MagicMock()
    monkeypatch.setattr(admin.sources_repo, "delete_source", delete_mock)

    resp = client.post("/admin/sources/9/delete", data={"csrf_token": csrf_token}, follow_redirects=False)
    assert resp.status_code == 303
    delete_mock.assert_called_once_with(delete_mock.call_args.args[0], 9)


def test_delete_missing_source_is_404(client, csrf_token, monkeypatch):
    _login(client)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=None))
    delete_mock = MagicMock()
    monkeypatch.setattr(admin.sources_repo, "delete_source", delete_mock)
    resp = client.post("/admin/sources/999/delete", data={"csrf_token": csrf_token})
    assert resp.status_code == 404
    delete_mock.assert_not_called()


# --- Approve / reject ----------------------------------------------------------------------


def test_approve_flips_pending_to_active(client, csrf_token, monkeypatch):
    _login(client)
    record = _make_record(id=3, name="proposed", status="pending", proposed_by="agent-x")
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    status_mock = MagicMock()
    monkeypatch.setattr(admin.sources_repo, "set_status", status_mock)

    resp = client.post("/admin/sources/3/approve", data={"csrf_token": csrf_token}, follow_redirects=False)
    assert resp.status_code == 303
    status_mock.assert_called_once_with(status_mock.call_args.args[0], 3, "active")


def test_reject_flips_pending_to_rejected(client, csrf_token, monkeypatch):
    _login(client)
    record = _make_record(id=3, name="proposed", status="pending", proposed_by="agent-x")
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    status_mock = MagicMock()
    monkeypatch.setattr(admin.sources_repo, "set_status", status_mock)

    resp = client.post("/admin/sources/3/reject", data={"csrf_token": csrf_token}, follow_redirects=False)
    assert resp.status_code == 303
    status_mock.assert_called_once_with(status_mock.call_args.args[0], 3, "rejected")


# --- Manual sync ---------------------------------------------------------------------------


def test_sync_triggers_exactly_one_sync_call(client, csrf_token, monkeypatch):
    _login(client)
    record = _make_record(id=5, name="widget", status="active")
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    outcome = SourceOutcome(name="widget", status="ok", pages_fetched=3, chunks_indexed=10)
    sync_mock = MagicMock(return_value=outcome)
    monkeypatch.setattr(admin.store, "sync_source", sync_mock)

    resp = client.post("/admin/sources/5/sync", data={"csrf_token": csrf_token}, follow_redirects=False)
    assert resp.status_code == 303
    sync_mock.assert_called_once()


def test_sync_returns_409_with_message_when_lock_held(client, csrf_token, monkeypatch):
    _login(client)
    record = _make_record(id=5, name="widget", status="active")
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    sync_mock = MagicMock()
    monkeypatch.setattr(admin.store, "sync_source", sync_mock)

    # Simulate the lock already being held by another in-flight sync.
    assert admin._manual_sync_lock.acquire(blocking=False)
    try:
        resp = client.post("/admin/sources/5/sync", data={"csrf_token": csrf_token})
        assert resp.status_code == 409
        assert "already running" in resp.text.lower()
        # Must be a rendered HTML message, not a raw traceback/JSON error dump.
        assert "Traceback" not in resp.text
        sync_mock.assert_not_called()
    finally:
        admin._manual_sync_lock.release()


def test_sync_refuses_non_active_source(client, csrf_token, monkeypatch):
    _login(client)
    record = _make_record(id=5, name="widget", status="pending")
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    sync_mock = MagicMock()
    monkeypatch.setattr(admin.store, "sync_source", sync_mock)

    resp = client.post("/admin/sources/5/sync", data={"csrf_token": csrf_token})
    assert resp.status_code == 409
    sync_mock.assert_not_called()


def test_sync_missing_source_is_404(client, csrf_token, monkeypatch):
    _login(client)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=None))
    resp = client.post("/admin/sources/999/sync", data={"csrf_token": csrf_token})
    assert resp.status_code == 404


# --- Pure helper unit tests (no DB, no HTTP) ------------------------------------------------


def test_split_prefixes_handles_blank_lines_and_commas():
    assert admin._split_prefixes("/docs/\n\n/api/, /tutorial/\n  ") == ["/docs/", "/api/", "/tutorial/"]


def test_build_source_config_valid():
    cfg, error = admin._build_source_config(
        name="widget",
        base_url="https://widget.example.com/docs/",
        sitemap="",
        include_prefixes="",
        exclude_prefixes="",
        max_pages="50",
        language="english",
        rate_limit_rps="1.0",
    )
    assert error is None
    assert isinstance(cfg, SourceConfig)
    assert cfg.max_pages == 50


def test_build_source_config_invalid_returns_error_not_raise():
    cfg, error = admin._build_source_config(
        name="widget",
        base_url="not-a-url",
        sitemap="",
        include_prefixes="",
        exclude_prefixes="",
        max_pages="50",
        language="english",
        rate_limit_rps="1.0",
    )
    assert cfg is None
    assert error is not None


def test_record_to_config_roundtrip():
    record = _make_record(name="widget", base_url="https://widget.example.com/docs/", max_pages=10)
    cfg = admin._record_to_config(record)
    assert isinstance(cfg, SourceConfig)
    assert cfg.name == "widget"
    assert cfg.max_pages == 10


def test_list_docs_view(client, csrf_token, monkeypatch):
    _login(client)
    page_rec = PageRecord(
        id=1,
        source_id=5,
        source_name="widget",
        url="https://widget.example.com/docs/guide",
        content_hash="abc123hash",
        fetched_at=datetime.now(timezone.utc),
        chunk_count=4,
    )
    monkeypatch.setattr(admin.store, "list_doc_pages", MagicMock(return_value=[page_rec]))
    monkeypatch.setattr(admin.sources_repo, "list_sources", MagicMock(return_value=[_make_record(id=5, name="widget")]))

    resp = client.get("/admin/docs?source_id=5&query=guide")
    assert resp.status_code == 200
    assert "Knowledge Base Browser" in resp.text
    assert "widget" in resp.text
    assert "guide" in resp.text


def test_get_page_chunks_view(client, monkeypatch):
    _login(client)
    chunk_rec = ChunkRecord(
        id=101,
        heading_path="Guide > Routing",
        chunk_index=0,
        content="# Routing\nDynamic routes work by...",
    )
    monkeypatch.setattr(admin.store, "get_page_chunks", MagicMock(return_value=[chunk_rec]))

    resp = client.get("/admin/docs/pages/1/chunks")
    assert resp.status_code == 200
    assert "Guide &gt; Routing" in resp.text
    assert "Dynamic routes work by..." in resp.text


def test_sync_all_submit(client, csrf_token, monkeypatch):
    _login(client)
    active_sources = [
        _make_record(id=1, name="src-a", status="active"),
        _make_record(id=2, name="src-b", status="active"),
    ]
    monkeypatch.setattr(admin.sources_repo, "list_sources", MagicMock(return_value=active_sources))
    sync_mock = MagicMock()
    monkeypatch.setattr(admin.store, "sync_source", sync_mock)

    resp = client.post("/admin/sync-all", data={"csrf_token": csrf_token}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin?msg=full_sync_completed"
    assert sync_mock.call_count == 2


def test_sync_target_submit(client, csrf_token, monkeypatch):
    _login(client)
    record = _make_record(id=5, name="widget", status="active")
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    outcome = SourceOutcome(name="widget", status="ok", pages_fetched=3, chunks_indexed=10)
    sync_mock = MagicMock(return_value=outcome)
    monkeypatch.setattr(admin.store, "sync_source", sync_mock)

    resp = client.post("/admin/sources/sync-target", data={"source_id": "5", "csrf_token": csrf_token}, follow_redirects=False)
    assert resp.status_code == 303
    assert "synced+widget:+ok" in resp.headers["location"]
    sync_mock.assert_called_once()


def test_store_row_records():
    now = datetime.now(timezone.utc)
    page = admin.store._row_to_page_record((1, 5, "widget", "https://example.com", "hash123", now, 3))
    assert page.id == 1
    assert page.source_name == "widget"
    assert page.chunk_count == 3

    chunk = admin.store._row_to_chunk_record((10, "Intro", 0, "Welcome"))
    assert chunk.id == 10
    assert chunk.heading_path == "Intro"
    assert chunk.content == "Welcome"


def test_sync_status_widget_and_clear(client, csrf_token):
    _login(client)
    # 1. Test widget when idle
    admin._sync_status.clear()
    admin._sync_status["running"] = False
    resp = client.get("/admin/sync-status-widget")
    assert resp.status_code == 200
    assert 'id="sync-status-widget"' in resp.text
    assert 'style="display: none;"' in resp.text

    # 2. Test widget when running
    admin._sync_status["running"] = True
    admin._sync_status["source"] = "Test Source"
    admin._sync_status["pages_fetched"] = 12
    admin._sync_status["chunks_indexed"] = 34
    admin._sync_status["last_url"] = "https://example.com/doc"
    resp = client.get("/admin/sync-status-widget")
    assert resp.status_code == 200
    assert "Active Sync in Progress" in resp.text
    assert "Test Source" in resp.text
    assert "12" in resp.text
    assert "34" in resp.text
    assert "https://example.com/doc" in resp.text

    # 3. Test widget with completed summary
    admin._sync_status["running"] = False
    admin._sync_status["last_completed_summary"] = {
        "source": "Test Source",
        "status": "ok",
        "pages_fetched": 12,
        "chunks_indexed": 34,
        "pages_skipped": 5,
        "pages_failed": 0,
        "error": None,
        "finished_at": time.time(),
    }
    resp = client.get("/admin/sync-status-widget")
    assert resp.status_code == 200
    assert "Operation Status: SUCCESS" in resp.text
    assert "Dismiss" in resp.text

    # 4. Test dismiss / clear endpoint
    resp = client.post("/admin/sync-status/clear", data={"csrf_token": csrf_token})
    assert resp.status_code == 200
    assert "last_completed_summary" not in admin._sync_status
    assert 'style="display: none;"' in resp.text
