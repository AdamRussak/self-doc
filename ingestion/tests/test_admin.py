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

import re
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock

import psycopg
import pytest
from app import admin
from app.config import SourceConfig
from app.sources_repo import SourceRecord
from app.store import ChunkRecord, PageRecord, SourceOutcome
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

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
        js_render=False,
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


def test_create_without_name_returns_422(client, csrf_token, monkeypatch):
    """POST without a name field must be rejected with 422 unprocessable entity,
    as name and base_url are both required fields."""
    _login(client)
    create_mock = MagicMock()
    monkeypatch.setattr(admin.sources_repo, "create_source", create_mock)

    resp = client.post(
        "/admin/sources/new",
        data={"csrf_token": csrf_token, "base_url": "https://docs.widget.example.com/guide/"},
        follow_redirects=False,
    )
    assert resp.status_code == 422
    create_mock.assert_not_called()


def test_create_name_and_base_url_only_succeeds(client, csrf_token, monkeypatch):
    """POST with only name and base_url — the primary fields — must succeed
    and create an active source."""
    _login(client)
    create_mock = MagicMock(return_value=42)
    monkeypatch.setattr(admin.sources_repo, "create_source", create_mock)

    resp = client.post(
        "/admin/sources/new",
        data={
            "csrf_token": csrf_token,
            "name": "widget-docs",
            "base_url": "https://docs.widget.example.com/guide/",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    create_mock.assert_called_once()
    _conn, cfg = create_mock.call_args.args
    assert isinstance(cfg, SourceConfig)
    assert cfg.name == "widget-docs"


def test_create_duplicate_name_returns_400_not_500(client, csrf_token, monkeypatch):
    _login(client)
    monkeypatch.setattr(
        admin.sources_repo,
        "create_source",
        MagicMock(side_effect=psycopg.errors.UniqueViolation("duplicate key value violates unique constraint")),
    )

    resp = client.post(
        "/admin/sources/new",
        data={
            "csrf_token": csrf_token,
            "name": "widget",
            "base_url": "https://widget.example.com/docs/",
            "max_pages": "100",
        },
    )
    assert resp.status_code == 400
    assert "already exists" in resp.text.lower()


# --- Create: source_type='upload' (T8) ------------------------------------------------------


def test_new_source_form_renders_source_type_radios(client, monkeypatch):
    """The CREATE form must offer a source-type selector, defaulted to
    'crawl', that is absent on the EDIT form (source_type is immutable
    after creation)."""
    _login(client)
    resp = client.get("/admin/sources/new")
    assert resp.status_code == 200
    assert 'name="source_type" value="crawl" checked' in resp.text
    assert 'name="source_type" value="upload"' in resp.text


def test_edit_form_does_not_render_source_type_radio(client, monkeypatch):
    _login(client)
    record = _make_record(id=7, name="widget")
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))

    resp = client.get("/admin/sources/7")
    assert resp.status_code == 200
    assert 'name="source_type"' not in resp.text


def test_create_upload_source_with_just_name_succeeds(client, csrf_token, monkeypatch):
    """POSTing only name + source_type='upload' (no base_url at all) must
    succeed and synthesize base_url='upload://{name}'."""
    _login(client)
    create_mock = MagicMock(return_value=42)
    monkeypatch.setattr(admin.sources_repo, "create_source", create_mock)

    resp = client.post(
        "/admin/sources/new",
        data={"csrf_token": csrf_token, "name": "x", "source_type": "upload"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    create_mock.assert_called_once()
    _conn, cfg = create_mock.call_args.args
    assert isinstance(cfg, SourceConfig)
    assert cfg.source_type == "upload"
    assert cfg.base_url == "upload://x"


def test_create_upload_source_ignores_submitted_base_url(client, csrf_token, monkeypatch):
    """A submitted base_url is never trusted for source_type='upload' — the
    sentinel is always synthesized from name, defense in depth against a
    hand-crafted POST bypassing the hidden field."""
    _login(client)
    create_mock = MagicMock(return_value=42)
    monkeypatch.setattr(admin.sources_repo, "create_source", create_mock)

    resp = client.post(
        "/admin/sources/new",
        data={
            "csrf_token": csrf_token,
            "name": "x",
            "source_type": "upload",
            "base_url": "https://evil.example.com/",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    _conn, cfg = create_mock.call_args.args
    assert cfg.base_url == "upload://x"


def test_create_upload_without_name_returns_422(client, csrf_token, monkeypatch):
    """The required-name 422 behavior from bbd4255 fires for source_type='upload'
    too, not just the default 'crawl' path."""
    _login(client)
    create_mock = MagicMock()
    monkeypatch.setattr(admin.sources_repo, "create_source", create_mock)

    resp = client.post(
        "/admin/sources/new",
        data={"csrf_token": csrf_token, "source_type": "upload"},
        follow_redirects=False,
    )
    assert resp.status_code == 422
    create_mock.assert_not_called()


def test_create_missing_source_type_defaults_to_crawl(client, csrf_token, monkeypatch):
    """Backward safety: an omitted source_type field must behave exactly
    like the pre-existing 'crawl' default path."""
    _login(client)
    create_mock = MagicMock(return_value=42)
    monkeypatch.setattr(admin.sources_repo, "create_source", create_mock)

    resp = client.post(
        "/admin/sources/new",
        data={
            "csrf_token": csrf_token,
            "name": "widget",
            "base_url": "https://widget.example.com/docs/",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    _conn, cfg = create_mock.call_args.args
    assert cfg.source_type == "crawl"
    assert cfg.base_url == "https://widget.example.com/docs/"


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


# --- T19_FIX: crawl-only entrypoints must refuse upload-type sources cleanly -------------


def test_sync_upload_source_returns_409_no_lock_leak(client, csrf_token, monkeypatch):
    """The original bug (T19): POSTing to the manual-sync route for an
    upload-type source used to build an invalid `SourceConfig`
    (`_record_to_config` didn't pass `source_type` through), raise a
    `pydantic.ValidationError` -> uncaught 500, AFTER the sync lock had
    already been acquired -- permanently leaking it. Must now be a clean
    409, with the lock never even acquired (checked before
    `try_acquire_sync_lock()`, not after)."""
    _login(client)
    record = _make_record(id=7, name="widget-uploads", source_type="upload", base_url="upload://widget-uploads")
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    sync_mock = MagicMock()
    monkeypatch.setattr(admin.store, "sync_source", sync_mock)
    record_to_config_spy = MagicMock(wraps=admin._record_to_config)
    monkeypatch.setattr(admin, "_record_to_config", record_to_config_spy)

    assert not admin._manual_sync_lock.locked()
    resp = client.post("/admin/sources/7/sync", data={"csrf_token": csrf_token})

    assert resp.status_code == 409
    assert "upload" in resp.text.lower()
    assert "Traceback" not in resp.text
    sync_mock.assert_not_called()
    # The lock must never have been touched at all for this rejection -- not
    # just released, never acquired in the first place.
    record_to_config_spy.assert_not_called()
    assert not admin._manual_sync_lock.locked()


def test_refresh_upload_source_returns_409_no_purge_no_lock_leak(client, csrf_token, monkeypatch):
    """The original bug (T19): `refresh_source_submit` acquired the lock,
    then called `store.purge_source()` (deleting the upload source's
    doc_pages/doc_chunks) and committed BEFORE `_record_to_config` raised --
    permanently destroying uploaded content with no crawl to re-fetch it,
    plus leaking the lock. Must now be a clean 409 with purge/commit never
    reached and the lock never acquired."""
    _login(client)
    record = _make_record(id=7, name="widget-uploads", source_type="upload", base_url="upload://widget-uploads")
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    purge_mock = MagicMock()
    monkeypatch.setattr(admin.store, "purge_source", purge_mock)
    sync_mock = MagicMock()
    monkeypatch.setattr(admin.store, "sync_source", sync_mock)

    assert not admin._manual_sync_lock.locked()
    resp = client.post("/admin/sources/7/refresh", data={"csrf_token": csrf_token})

    assert resp.status_code == 409
    assert "upload" in resp.text.lower()
    assert "Traceback" not in resp.text
    purge_mock.assert_not_called()
    sync_mock.assert_not_called()
    assert not admin._manual_sync_lock.locked()


# --- Upload (T9) -------------------------------------------------------------------------


def _upload_record(**overrides) -> SourceRecord:
    defaults = dict(source_type="upload", base_url="upload://widget-uploads", name="widget-uploads")
    defaults.update(overrides)
    return _make_record(**defaults)


def test_upload_rejects_unauthenticated(client):
    resp = client.post("/admin/sources/1/upload", files={"files": ("a.md", b"# hi", "text/markdown")})
    assert resp.status_code == 401


def test_upload_rejects_missing_csrf_token(client):
    _login(client)
    resp = client.post("/admin/sources/1/upload", files={"files": ("a.md", b"# hi", "text/markdown")})
    assert resp.status_code == 403


def test_upload_rejects_wrong_csrf_token(client):
    _login(client)
    resp = client.post(
        "/admin/sources/1/upload",
        data={"csrf_token": "not-the-right-value"},
        files={"files": ("a.md", b"# hi", "text/markdown")},
    )
    assert resp.status_code == 403


def test_upload_against_crawl_source_is_409(client, csrf_token, monkeypatch):
    _login(client)
    record = _make_record(id=1, name="widget", source_type="crawl")
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    ingest_mock = MagicMock()
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", ingest_mock)

    resp = client.post(
        "/admin/sources/1/upload",
        data={"csrf_token": csrf_token},
        files={"files": ("a.md", b"# hi", "text/markdown")},
    )
    assert resp.status_code == 409
    ingest_mock.assert_not_called()


def test_upload_missing_source_is_404(client, csrf_token, monkeypatch):
    _login(client)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=None))
    resp = client.post(
        "/admin/sources/999/upload",
        data={"csrf_token": csrf_token},
        files={"files": ("a.md", b"# hi", "text/markdown")},
    )
    assert resp.status_code == 404


def test_upload_returns_409_with_message_when_lock_held(client, csrf_token, monkeypatch):
    _login(client)
    record = _upload_record(id=5)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    ingest_mock = MagicMock()
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", ingest_mock)

    # Simulate the lock already being held by another in-flight sync/upload.
    assert admin._manual_sync_lock.acquire(blocking=False)
    try:
        resp = client.post(
            "/admin/sources/5/upload",
            data={"csrf_token": csrf_token},
            files={"files": ("a.md", b"# hi", "text/markdown")},
        )
        assert resp.status_code == 409
        assert "already" in resp.text.lower()
        assert "Traceback" not in resp.text
        ingest_mock.assert_not_called()
        # The route must not further acquire (or release) the lock: it
        # remains held by this test's own acquire, untouched.
        assert admin._manual_sync_lock.locked()
    finally:
        admin._manual_sync_lock.release()


def test_upload_parser_exception_still_releases_lock(client, csrf_token, monkeypatch):
    """A non-UploadError exception raised mid-parse (e.g. a genuine parser
    bug) must not leak the sync lock: the `finally` in `upload_source_submit`
    releases it even though the exception itself propagates."""
    _login(client)
    record = _upload_record(id=5)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))

    release_spy = MagicMock(side_effect=admin.release_sync_lock)
    monkeypatch.setattr(admin, "release_sync_lock", release_spy)
    monkeypatch.setattr(admin.uploads, "parse_upload", MagicMock(side_effect=RuntimeError("parser exploded")))

    with pytest.raises(RuntimeError, match="parser exploded"):
        client.post(
            "/admin/sources/5/upload",
            data={"csrf_token": csrf_token},
            files={"files": ("a.md", b"# hi", "text/markdown")},
        )

    release_spy.assert_called_once()
    assert not admin._manual_sync_lock.locked()


def test_upload_all_files_fail_rerenders_form_with_first_error(client, csrf_token, monkeypatch):
    _login(client)
    record = _upload_record(id=5)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    ingest_mock = MagicMock()
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", ingest_mock)

    def fake_parse_upload(filename, data):
        raise admin.UploadError("could not read that file")

    monkeypatch.setattr(admin.uploads, "parse_upload", fake_parse_upload)

    resp = client.post(
        "/admin/sources/5/upload",
        data={"csrf_token": csrf_token},
        files={"files": ("a.txt", b"whatever", "text/plain")},
    )
    assert resp.status_code == 400
    assert "could not read that file" in resp.text
    ingest_mock.assert_not_called()
    assert not admin._manual_sync_lock.locked()


def test_upload_happy_path_calls_ingest_and_redirects(client, csrf_token, monkeypatch):
    _login(client)
    record = _upload_record(id=5)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    outcome = SourceOutcome(name="widget-uploads", status="ok", pages_fetched=1, chunks_indexed=3)
    ingest_mock = MagicMock(return_value=outcome)
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", ingest_mock)

    resp = client.post(
        "/admin/sources/5/upload",
        data={"csrf_token": csrf_token},
        files={"files": ("a.md", b"# hello world", "text/markdown")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["hx-trigger"] == "syncStatusUpdated"
    ingest_mock.assert_called_once()
    call_args = ingest_mock.call_args
    assert call_args.args[1] is record
    docs = call_args.args[2]
    assert len(docs) == 1
    assert docs[0].markdown == "# hello world"
    assert not admin._manual_sync_lock.locked()


# --- Create + upload in one submit (T23/T24) ------------------------------------------------
#
# The create form is now multipart and carries an optional file input, so a
# source_type='upload' source can be created AND populated by a single POST
# to /admin/sources/new. The redirect contract under test:
#
#   crawl (files ignored)      -> /admin?msg=created+{name}          (unchanged)
#   upload, no files           -> /admin/sources/{id}?msg=created+{name}
#   upload, inline ingest      -> /admin/sources/{id}?msg=created+{name}:+{status}  + HX-Trigger
#   upload, background handoff -> /admin/sources/{id}?msg=upload_started+{name}     + HX-Trigger
#   upload, nothing parsable   -> /admin/sources/{id}?msg=created+{name}+—+upload+failed:+...
#   upload, lock busy          -> /admin/sources/{id}?msg=created+{name}+—+upload+skipped:+...
#
# In every failure case the source row SURVIVES (it is never deleted or
# rolled back) and nothing 500s. Under pytest, `PYTEST_CURRENT_TEST` is set,
# so the route takes the inline-ingest branch unless a test removes it.

_BOUNDARY = "----selfdocstestboundary"


def _raw_multipart(fields: list[tuple[str, str]], file_parts: list[tuple[str, str, bytes, str]]) -> tuple[bytes, dict[str, str]]:
    """Hand-build a multipart/form-data body, returning (body, headers).

    Needed because httpx's `files={"files": ("", b"", "text/plain")}`
    shorthand OMITS the `filename` parameter entirely when the filename is
    empty — Starlette then parses that part as a plain `str` form field and
    FastAPI 422s the request BEFORE the route body ever runs. A real browser
    submitting an untouched `<input type="file">` sends the parameter
    present-but-empty (`filename=""`), which Starlette parses as an
    `UploadFile` with `filename == ""` and `_parse_upload_files` skips. Only
    a hand-built body can express that, so only a hand-built body actually
    tests the case a browser produces.

    `file_parts` entries are (field_name, filename, content, content_type);
    `filename` is emitted verbatim, empty string included.
    """
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.append(f"--{_BOUNDARY}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(value.encode() + b"\r\n")
    for name, filename, content, content_type in file_parts:
        chunks.append(f"--{_BOUNDARY}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode())
        chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode())
        chunks.append(content + b"\r\n")
    chunks.append(f"--{_BOUNDARY}--\r\n".encode())
    return b"".join(chunks), {"Content-Type": f"multipart/form-data; boundary={_BOUNDARY}"}


def test_raw_multipart_helper_produces_a_browser_style_empty_file_part():
    """Guard on the test helper itself: if it ever stopped emitting a literal
    `filename=""`, the empty-part tests below would silently start testing
    httpx's (different, 422-producing) shorthand instead."""
    body, headers = _raw_multipart([("name", "x")], [("files", "", b"", "application/octet-stream")])
    assert b'name="files"; filename=""' in body
    assert headers["Content-Type"].startswith("multipart/form-data; boundary=")


# (a) upload + a parsable file: create, then ingest inline, in one submit.


def test_create_upload_with_file_ingests_once_and_redirects_to_the_new_source(client, csrf_token, monkeypatch):
    _login(client)
    create_mock = MagicMock(return_value=42)
    monkeypatch.setattr(admin.sources_repo, "create_source", create_mock)
    record = _upload_record(id=42, name="widget-uploads")
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    outcome = SourceOutcome(name="widget-uploads", status="ok", pages_fetched=1, chunks_indexed=3)
    ingest_mock = MagicMock(return_value=outcome)
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", ingest_mock)

    resp = client.post(
        "/admin/sources/new",
        data={"csrf_token": csrf_token, "name": "widget-uploads", "source_type": "upload"},
        files={"files": ("a.md", b"# hello world", "text/markdown")},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/admin/sources/42?")
    assert "msg=created+widget-uploads" in location
    assert "ok" in location  # the inline outcome.status is surfaced
    assert resp.headers["hx-trigger"] == "syncStatusUpdated"

    create_mock.assert_called_once()
    _conn, cfg = create_mock.call_args.args
    assert cfg.source_type == "upload"

    ingest_mock.assert_called_once()
    assert ingest_mock.call_args.args[1] is record
    docs = ingest_mock.call_args.args[2]
    assert len(docs) == 1
    assert docs[0].markdown == "# hello world"
    assert not admin._manual_sync_lock.locked()


def test_create_upload_with_multiple_files_ingests_all_of_them_in_one_call(client, csrf_token, monkeypatch):
    """The file input is `multiple` — every attached file must land in a
    single ingest call, not one call per file (which would re-run the
    whole ingest pipeline N times)."""
    _login(client)
    monkeypatch.setattr(admin.sources_repo, "create_source", MagicMock(return_value=42))
    record = _upload_record(id=42, name="widget-uploads")
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    outcome = SourceOutcome(name="widget-uploads", status="ok", pages_fetched=2, chunks_indexed=4)
    ingest_mock = MagicMock(return_value=outcome)
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", ingest_mock)

    resp = client.post(
        "/admin/sources/new",
        data={"csrf_token": csrf_token, "name": "widget-uploads", "source_type": "upload"},
        files=[
            ("files", ("a.md", b"# first", "text/markdown")),
            ("files", ("b.md", b"# second", "text/markdown")),
        ],
        follow_redirects=False,
    )

    assert resp.status_code == 303
    ingest_mock.assert_called_once()
    docs = ingest_mock.call_args.args[2]
    assert [doc.markdown for doc in docs] == ["# first", "# second"]
    assert not admin._manual_sync_lock.locked()


# (b) upload with no file part at all: create the source, ingest nothing.


def test_create_upload_without_any_file_part_creates_source_and_skips_ingest(client, csrf_token, monkeypatch):
    """The "create it now, upload into it later" path, and every API caller
    that posts no file part at all."""
    _login(client)
    create_mock = MagicMock(return_value=42)
    monkeypatch.setattr(admin.sources_repo, "create_source", create_mock)
    ingest_mock = MagicMock()
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", ingest_mock)

    resp = client.post(
        "/admin/sources/new",
        data={"csrf_token": csrf_token, "name": "widget-uploads", "source_type": "upload"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/sources/42?msg=created+widget-uploads"
    assert "hx-trigger" not in resp.headers
    create_mock.assert_called_once()
    ingest_mock.assert_not_called()
    assert not admin._manual_sync_lock.locked()


# (c) upload with the empty part a browser sends for an untouched file input.


def test_create_upload_with_browser_empty_file_part_is_treated_as_no_files(client, csrf_token, monkeypatch):
    """A browser always submits the file input, even untouched — as a part
    with `filename=""`. That must be indistinguishable from "no files
    attached": source created, nothing ingested, and emphatically NOT a
    422 (which is what a `File(...)`-required parameter would produce)."""
    _login(client)
    create_mock = MagicMock(return_value=42)
    monkeypatch.setattr(admin.sources_repo, "create_source", create_mock)
    ingest_mock = MagicMock()
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", ingest_mock)

    body, headers = _raw_multipart(
        [("csrf_token", csrf_token), ("name", "widget-uploads"), ("source_type", "upload")],
        [("files", "", b"", "application/octet-stream")],
    )
    resp = client.post("/admin/sources/new", content=body, headers=headers, follow_redirects=False)

    assert resp.status_code == 303, resp.text  # not 422: the route really ran
    assert resp.headers["location"] == "/admin/sources/42?msg=created+widget-uploads"
    create_mock.assert_called_once()
    ingest_mock.assert_not_called()
    assert not admin._manual_sync_lock.locked()


def test_create_upload_with_whitespace_only_filename_is_treated_as_no_files(client, csrf_token, monkeypatch):
    """Boundary neighbour of the empty part: a filename of only whitespace is
    skipped too (`filename.strip()`), so it can never reach a parser as a
    bogus "unsupported file type: ' '" error."""
    _login(client)
    monkeypatch.setattr(admin.sources_repo, "create_source", MagicMock(return_value=42))
    ingest_mock = MagicMock()
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", ingest_mock)

    body, headers = _raw_multipart(
        [("csrf_token", csrf_token), ("name", "widget-uploads"), ("source_type", "upload")],
        [("files", "   ", b"", "application/octet-stream")],
    )
    resp = client.post("/admin/sources/new", content=body, headers=headers, follow_redirects=False)

    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/admin/sources/42?msg=created+widget-uploads"
    ingest_mock.assert_not_called()


# (d) crawl create submitted as multipart: byte-identical to the old behavior.


def test_create_crawl_multipart_with_stray_empty_file_part_redirects_to_index(client, csrf_token, monkeypatch):
    """Every crawl create from the (now multipart) form carries the untouched
    file input's empty part. It must be ignored outright — same
    `/admin?msg=created+{name}` redirect as before this feature existed, no
    ingest, no 422."""
    _login(client)
    create_mock = MagicMock(return_value=42)
    monkeypatch.setattr(admin.sources_repo, "create_source", create_mock)
    ingest_mock = MagicMock()
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", ingest_mock)

    body, headers = _raw_multipart(
        [
            ("csrf_token", csrf_token),
            ("name", "widget"),
            ("source_type", "crawl"),
            ("base_url", "https://widget.example.com/docs/"),
        ],
        [("files", "", b"", "application/octet-stream")],
    )
    resp = client.post("/admin/sources/new", content=body, headers=headers, follow_redirects=False)

    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/admin?msg=created+widget"
    create_mock.assert_called_once()
    _conn, cfg = create_mock.call_args.args
    assert cfg.source_type == "crawl"
    assert cfg.base_url == "https://widget.example.com/docs/"
    ingest_mock.assert_not_called()
    assert not admin._manual_sync_lock.locked()


def test_create_crawl_multipart_ignores_an_actually_attached_file(client, csrf_token, monkeypatch):
    """Defense in depth: even a real, parsable file riding along on a
    crawl-type create is ignored — a crawl source gets its content by
    crawling, and silently ingesting an upload into one would leave content
    the next sync cannot reproduce."""
    _login(client)
    monkeypatch.setattr(admin.sources_repo, "create_source", MagicMock(return_value=42))
    ingest_mock = MagicMock()
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", ingest_mock)

    resp = client.post(
        "/admin/sources/new",
        data={
            "csrf_token": csrf_token,
            "name": "widget",
            "source_type": "crawl",
            "base_url": "https://widget.example.com/docs/",
        },
        files={"files": ("a.md", b"# sneaky", "text/markdown")},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin?msg=created+widget"
    ingest_mock.assert_not_called()


def test_create_crawl_multipart_still_validates_base_url(client, csrf_token, monkeypatch):
    """Switching the form to multipart must not weaken validation: a bad
    base_url still re-renders the form with a 400 and writes nothing."""
    _login(client)
    create_mock = MagicMock()
    monkeypatch.setattr(admin.sources_repo, "create_source", create_mock)

    body, headers = _raw_multipart(
        [
            ("csrf_token", csrf_token),
            ("name", "widget"),
            ("source_type", "crawl"),
            ("base_url", "not-a-url"),
        ],
        [("files", "", b"", "application/octet-stream")],
    )
    resp = client.post("/admin/sources/new", content=body, headers=headers, follow_redirects=False)

    assert resp.status_code == 400, resp.text
    assert "error" in resp.text.lower()
    create_mock.assert_not_called()


# (e) upload where nothing parses: the source row still survives.


def test_create_upload_with_unparsable_file_still_creates_the_source(client, csrf_token, monkeypatch):
    """An unsupported file type yields no docs and no per-file error, so the
    generic "nothing parsable" message is reported. The source row must
    survive untouched — never deleted, never rolled back — and the response
    must be a 303 to that source, not a 500."""
    _login(client)
    create_mock = MagicMock(return_value=42)
    monkeypatch.setattr(admin.sources_repo, "create_source", create_mock)
    delete_mock = MagicMock()
    monkeypatch.setattr(admin.sources_repo, "delete_source", delete_mock)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=_upload_record(id=42, name="widget-uploads")))
    ingest_mock = MagicMock()
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", ingest_mock)

    resp = client.post(
        "/admin/sources/new",
        data={"csrf_token": csrf_token, "name": "widget-uploads", "source_type": "upload"},
        files={"files": ("payload.exe", b"\x00\x01\x02garbage", "application/octet-stream")},
        follow_redirects=False,
    )

    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    assert location.startswith("/admin/sources/42?")
    assert "msg=created+widget-uploads" in location
    assert "upload+failed" in location
    assert "No+parsable+content" in location
    create_mock.assert_called_once()
    delete_mock.assert_not_called()
    ingest_mock.assert_not_called()
    assert not admin._manual_sync_lock.locked()


def test_create_upload_with_corrupt_zip_reports_the_parser_error_and_keeps_the_source(client, csrf_token, monkeypatch):
    """An `UploadError` from a parser is reported as the failure detail (not
    the generic message) and is never raised: still a 303, still no 500, row
    still created."""
    _login(client)
    create_mock = MagicMock(return_value=42)
    monkeypatch.setattr(admin.sources_repo, "create_source", create_mock)
    delete_mock = MagicMock()
    monkeypatch.setattr(admin.sources_repo, "delete_source", delete_mock)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=_upload_record(id=42, name="widget-uploads")))
    ingest_mock = MagicMock()
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", ingest_mock)

    resp = client.post(
        "/admin/sources/new",
        data={"csrf_token": csrf_token, "name": "widget-uploads", "source_type": "upload"},
        files={"files": ("bundle.zip", b"this is definitely not a zip", "application/zip")},
        follow_redirects=False,
    )

    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    assert location.startswith("/admin/sources/42?")
    assert "upload+failed" in location
    assert "not+a+valid+zip" in location
    create_mock.assert_called_once()
    delete_mock.assert_not_called()
    ingest_mock.assert_not_called()
    assert not admin._manual_sync_lock.locked()


def test_create_upload_unparsable_file_never_acquires_the_sync_lock(client, csrf_token, monkeypatch):
    """Parsing runs BEFORE the lock is taken, so an unparsable batch never
    contends for it at all — and, critically, the route must not release a
    lock it never acquired (that would stomp on whoever does hold it)."""
    _login(client)
    monkeypatch.setattr(admin.sources_repo, "create_source", MagicMock(return_value=42))
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=_upload_record(id=42, name="widget-uploads")))
    acquire_spy = MagicMock(wraps=admin.try_acquire_sync_lock)
    release_spy = MagicMock(wraps=admin.release_sync_lock)
    monkeypatch.setattr(admin, "try_acquire_sync_lock", acquire_spy)
    monkeypatch.setattr(admin, "release_sync_lock", release_spy)

    resp = client.post(
        "/admin/sources/new",
        data={"csrf_token": csrf_token, "name": "widget-uploads", "source_type": "upload"},
        files={"files": ("payload.exe", b"garbage", "application/octet-stream")},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    acquire_spy.assert_not_called()
    release_spy.assert_not_called()
    assert not admin._manual_sync_lock.locked()


# (f) lock hygiene — the T19_FIX bug class: never leak, never steal.


def test_create_upload_when_lock_held_skips_upload_and_leaves_the_lock_with_its_owner(client, csrf_token, monkeypatch):
    """A sync already in flight owns the lock. The create still succeeds, the
    upload is skipped with a message, and the route must neither ingest nor
    RELEASE the lock it failed to acquire — releasing another holder's lock
    is the same class of bug as leaking one."""
    _login(client)
    create_mock = MagicMock(return_value=42)
    monkeypatch.setattr(admin.sources_repo, "create_source", create_mock)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=_upload_record(id=42, name="widget-uploads")))
    ingest_mock = MagicMock()
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", ingest_mock)

    assert admin._manual_sync_lock.acquire(blocking=False)
    try:
        resp = client.post(
            "/admin/sources/new",
            data={"csrf_token": csrf_token, "name": "widget-uploads", "source_type": "upload"},
            files={"files": ("a.md", b"# hello world", "text/markdown")},
            follow_redirects=False,
        )

        assert resp.status_code == 303, resp.text
        location = resp.headers["location"]
        assert location.startswith("/admin/sources/42?")
        assert "msg=created+widget-uploads" in location
        assert "upload+skipped" in location
        assert "sync+in+progress" in location
        create_mock.assert_called_once()
        ingest_mock.assert_not_called()
        # Still held by THIS test's acquire — the route did not steal it.
        assert admin._manual_sync_lock.locked()
    finally:
        admin._manual_sync_lock.release()


def test_create_upload_lock_busy_does_not_release_a_lock_it_never_acquired(client, csrf_token, monkeypatch):
    """Same contract as above, asserted at the seam rather than on the lock
    object: with `try_acquire_sync_lock` returning False, `release_sync_lock`
    must never be called."""
    _login(client)
    monkeypatch.setattr(admin.sources_repo, "create_source", MagicMock(return_value=42))
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=_upload_record(id=42, name="widget-uploads")))
    ingest_mock = MagicMock()
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", ingest_mock)
    monkeypatch.setattr(admin, "try_acquire_sync_lock", MagicMock(return_value=False))
    release_spy = MagicMock()
    monkeypatch.setattr(admin, "release_sync_lock", release_spy)

    resp = client.post(
        "/admin/sources/new",
        data={"csrf_token": csrf_token, "name": "widget-uploads", "source_type": "upload"},
        files={"files": ("a.md", b"# hello world", "text/markdown")},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert "upload+skipped" in resp.headers["location"]
    ingest_mock.assert_not_called()
    release_spy.assert_not_called()
    assert not admin._manual_sync_lock.locked()


def test_create_upload_ingest_exception_releases_the_lock_and_does_not_500(client, csrf_token, monkeypatch):
    """THE lock-leak shape (T19_FIX): the lock is acquired, then ingestion
    blows up. The row is already committed, so the route reports the failure
    as a redirect instead of a 500 — and the `finally` must still release the
    lock, or the whole admin UI deadlocks until restart."""
    _login(client)
    create_mock = MagicMock(return_value=42)
    monkeypatch.setattr(admin.sources_repo, "create_source", create_mock)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=_upload_record(id=42, name="widget-uploads")))
    release_spy = MagicMock(side_effect=admin.release_sync_lock)
    monkeypatch.setattr(admin, "release_sync_lock", release_spy)
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", MagicMock(side_effect=RuntimeError("ingest exploded")))

    resp = client.post(
        "/admin/sources/new",
        data={"csrf_token": csrf_token, "name": "widget-uploads", "source_type": "upload"},
        files={"files": ("a.md", b"# hello world", "text/markdown")},
        follow_redirects=False,
    )

    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    assert location.startswith("/admin/sources/42?")
    assert "upload+failed" in location
    assert "ingest+exploded" in location
    create_mock.assert_called_once()
    release_spy.assert_called_once()
    assert not admin._manual_sync_lock.locked()


def test_create_upload_parser_exception_releases_the_lock_and_does_not_500(client, csrf_token, monkeypatch):
    """A non-`UploadError` parser bug propagates out of `_parse_upload_files`.
    `upload_source_submit` lets it become a 500 (nothing was written there);
    here the row exists, so it is caught and reported — and no lock is
    leaked either way (none was acquired: parsing precedes the acquire)."""
    _login(client)
    monkeypatch.setattr(admin.sources_repo, "create_source", MagicMock(return_value=42))
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=_upload_record(id=42, name="widget-uploads")))
    ingest_mock = MagicMock()
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", ingest_mock)
    monkeypatch.setattr(admin.uploads, "parse_upload", MagicMock(side_effect=RuntimeError("parser exploded")))

    resp = client.post(
        "/admin/sources/new",
        data={"csrf_token": csrf_token, "name": "widget-uploads", "source_type": "upload"},
        files={"files": ("a.md", b"# hello world", "text/markdown")},
        follow_redirects=False,
    )

    assert resp.status_code == 303, resp.text
    assert "upload+failed" in resp.headers["location"]
    assert "parser+exploded" in resp.headers["location"]
    ingest_mock.assert_not_called()
    assert not admin._manual_sync_lock.locked()


def test_create_upload_missing_record_after_create_reports_failure_without_500(client, csrf_token, monkeypatch):
    """Can't-happen guard: `create_source` returned an id but re-reading it
    yields None (concurrent delete). Must degrade to the upload-failed
    redirect with the lock never acquired — not an AttributeError 500."""
    _login(client)
    monkeypatch.setattr(admin.sources_repo, "create_source", MagicMock(return_value=42))
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=None))
    ingest_mock = MagicMock()
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", ingest_mock)

    resp = client.post(
        "/admin/sources/new",
        data={"csrf_token": csrf_token, "name": "widget-uploads", "source_type": "upload"},
        files={"files": ("a.md", b"# hello world", "text/markdown")},
        follow_redirects=False,
    )

    assert resp.status_code == 303, resp.text
    assert resp.headers["location"].startswith("/admin/sources/42?")
    assert "upload+failed" in resp.headers["location"]
    ingest_mock.assert_not_called()
    assert not admin._manual_sync_lock.locked()


def test_create_upload_background_handoff_transfers_lock_ownership(client, csrf_token, monkeypatch):
    """Production path (no `PYTEST_CURRENT_TEST`): the route hands the parsed
    docs to `run_sync_task` and returns immediately. Lock ownership transfers
    to the worker — so the route must NOT release it in its `finally` (that
    would let a second sync start on top of the in-flight ingest)."""
    _login(client)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(admin.sources_repo, "create_source", MagicMock(return_value=42))
    record = _upload_record(id=42, name="widget-uploads")
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    ingest_mock = MagicMock()
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", ingest_mock)
    # Capture the handoff instead of running it: this test is about what the
    # REQUEST does with the lock, not what the worker later does with it.
    run_sync_task_mock = MagicMock()
    monkeypatch.setattr(admin, "run_sync_task", run_sync_task_mock)
    release_spy = MagicMock(side_effect=admin.release_sync_lock)
    monkeypatch.setattr(admin, "release_sync_lock", release_spy)

    try:
        resp = client.post(
            "/admin/sources/new",
            data={"csrf_token": csrf_token, "name": "widget-uploads", "source_type": "upload"},
            files={"files": ("a.md", b"# hello world", "text/markdown")},
            follow_redirects=False,
        )

        assert resp.status_code == 303, resp.text
        assert resp.headers["location"] == "/admin/sources/42?msg=upload_started+widget-uploads"
        assert resp.headers["hx-trigger"] == "syncStatusUpdated"
        run_sync_task_mock.assert_called_once()
        args = run_sync_task_mock.call_args.args
        assert args[0] is admin._bg_ingest_upload
        assert args[1] is record
        assert [doc.markdown for doc in args[2]] == ["# hello world"]
        assert args[4] == 42
        # No inline ingest on this path, and the lock stays held for the worker.
        ingest_mock.assert_not_called()
        release_spy.assert_not_called()
        assert admin._manual_sync_lock.locked()
    finally:
        if admin._manual_sync_lock.locked():
            admin._manual_sync_lock.release()


# (g) template wiring: the file input exists on CREATE only.


def _source_form_tag(html: str) -> str:
    match = re.search(r'<form id="source-form"[^>]*>', html)
    assert match is not None, "form#source-form not found in rendered page"
    return match.group(0)


def test_new_source_form_is_multipart_with_an_optional_file_input(client):
    _login(client)
    resp = client.get("/admin/sources/new")
    assert resp.status_code == 200

    assert 'enctype="multipart/form-data"' in _source_form_tag(resp.text)
    assert 'name="files"' in resp.text
    assert 'id="create-upload-files"' in resp.text
    # Pure-CSS progressive disclosure, mirroring the existing .crawl-only rules.
    assert '#source-form .upload-only' in resp.text
    assert '#source-form:has(input[name="source_type"][value="upload"]:checked) .upload-only' in resp.text


def test_new_source_file_input_is_not_required(client):
    """A `required` file input would block every crawl-type create the moment
    the browser un-hides it, and blocks the deliberate "create empty upload
    source now, populate later" flow."""
    _login(client)
    resp = client.get("/admin/sources/new")
    assert resp.status_code == 200
    match = re.search(r'<input type="file" id="create-upload-files"[^>]*>', resp.text)
    assert match is not None
    assert "required" not in match.group(0)


def test_edit_form_has_no_create_upload_file_input_or_enctype(client, monkeypatch):
    """The EDIT view is untouched by this feature: `update_source_submit` is
    still urlencoded, so an enctype on #source-form (or a stray `files` part)
    would 422 every save."""
    _login(client)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=_upload_record(id=7, name="widget-uploads")))

    resp = client.get("/admin/sources/7")
    assert resp.status_code == 200
    assert "enctype" not in _source_form_tag(resp.text)
    assert 'id="create-upload-files"' not in resp.text
    assert ".upload-only" not in resp.text
    # The pre-existing standing upload card on upload sources is unaffected.
    assert 'id="upload-files"' in resp.text
    assert 'action="/admin/sources/7/upload"' in resp.text


def test_edit_form_for_a_crawl_source_has_no_file_input_at_all(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=_make_record(id=7, name="widget")))

    resp = client.get("/admin/sources/7")
    assert resp.status_code == 200
    assert "enctype" not in _source_form_tag(resp.text)
    assert 'type="file"' not in resp.text
    assert 'name="files"' not in resp.text
    assert ".upload-only" not in resp.text


# (h) the redirect-carried ?msg= banner: does it RENDER, and in which colour? -----------------
#
# Two follow-up fixes live behind the redirect contract above, and NEITHER is
# provable from a `Location` header:
#
#   1. Every `?msg=` redirect that lands on a SOURCE page was silently
#      discarded — `_form_context` never read the param, so `admin/form.html`
#      rendered a perfectly ordinary-looking form and the operator was never
#      told the upload had failed. Asserting only on the redirect URL is
#      exactly what let that ship, so the tests below FOLLOW the redirect and
#      assert on the final rendered body.
#   2. `.message` is the green success style, so failures then rendered green.
#      Severity is now carried explicitly as `&level=warning`, whitelisted
#      server-side by `_message_level`, and adds the amber `.message-warning`
#      modifier to the banner's class attribute.
#
# `message-warning` also appears in base.html's <style> block on EVERY page, so
# a bare `"message-warning" in resp.text` would pass no matter what the banner
# says: these tests read the banner ELEMENT via `_rendered_banner` instead.

_BANNER_RE = re.compile(r'<div class="(?P<cls>message[^"]*)">(?P<text>.*?)</div>', re.DOTALL)


def _rendered_banner(html: str) -> tuple[str, str]:
    """(class attribute, inner text) of the rendered `?msg=` banner.

    Asserts the element exists, so "the banner silently disappeared" — the
    fix-round-1 bug — always fails loudly instead of vacuously passing."""
    match = _BANNER_RE.search(html)
    assert match is not None, "no ?msg= banner element rendered in the page"
    return match.group("cls"), match.group("text").strip()


def _assert_no_banner(html: str) -> None:
    match = _BANNER_RE.search(html)
    assert match is None, f"unexpected banner rendered: {match.group(0) if match else ''}"


def test_create_upload_failure_message_is_readable_on_the_page_it_lands_on(client, csrf_token, monkeypatch):
    """THE end-to-end guard: the failure detail is encoded into the redirect
    correctly *and* survives into the HTML the operator actually reads. A
    `Location`-header assertion passes even when the landing page drops the
    message on the floor, which is precisely how that bug shipped."""
    _login(client)
    monkeypatch.setattr(admin.sources_repo, "create_source", MagicMock(return_value=42))
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=_upload_record(id=42, name="widget-uploads")))
    ingest_mock = MagicMock()
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", ingest_mock)

    resp = client.post(
        "/admin/sources/new",
        data={"csrf_token": csrf_token, "name": "widget-uploads", "source_type": "upload"},
        files={"files": ("payload.exe", b"\x00\x01\x02garbage", "application/octet-stream")},
        follow_redirects=True,
    )

    assert resp.status_code == 200, resp.text
    assert resp.url.path == "/admin/sources/42"
    cls, text = _rendered_banner(resp.text)
    assert text == "created widget-uploads — upload failed: No parsable content found in the uploaded file(s)."
    assert cls.split() == ["message", "message-warning"]
    ingest_mock.assert_not_called()


def test_create_upload_success_message_renders_without_the_warning_modifier(client, csrf_token, monkeypatch):
    """The other half of the severity contract: a clean `status="ok"` ingest
    still gets a banner, and it keeps the plain green `.message` style. If
    everything rendered amber the modifier would carry no information."""
    _login(client)
    monkeypatch.setattr(admin.sources_repo, "create_source", MagicMock(return_value=42))
    record = _upload_record(id=42, name="widget-uploads")
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    outcome = SourceOutcome(name="widget-uploads", status="ok", pages_fetched=1, chunks_indexed=3)
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", MagicMock(return_value=outcome))

    resp = client.post(
        "/admin/sources/new",
        data={"csrf_token": csrf_token, "name": "widget-uploads", "source_type": "upload"},
        files={"files": ("a.md", b"# hello world", "text/markdown")},
        follow_redirects=True,
    )

    assert resp.status_code == 200, resp.text
    assert resp.url.path == "/admin/sources/42"
    cls, text = _rendered_banner(resp.text)
    assert text == "created widget-uploads: ok"
    assert cls == "message"


@pytest.mark.parametrize("status", ["partial", "failed", "some-future-status"])
def test_create_upload_non_ok_ingest_status_renders_a_warning_banner(client, csrf_token, monkeypatch, status):
    """`SourceOutcome.status` is `ok | partial | failed`; only `ok` is a clean
    success. The status rides in the message text, so a green
    "created widget-uploads: failed" would contradict itself. An unrecognised
    status warns too — over-warning is cosmetic, a false green is a lie."""
    _login(client)
    monkeypatch.setattr(admin.sources_repo, "create_source", MagicMock(return_value=42))
    record = _upload_record(id=42, name="widget-uploads")
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    outcome = SourceOutcome(name="widget-uploads", status=status, pages_fetched=1, chunks_indexed=0)
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", MagicMock(return_value=outcome))

    resp = client.post(
        "/admin/sources/new",
        data={"csrf_token": csrf_token, "name": "widget-uploads", "source_type": "upload"},
        files={"files": ("a.md", b"# hello world", "text/markdown")},
        follow_redirects=True,
    )

    assert resp.status_code == 200, resp.text
    cls, text = _rendered_banner(resp.text)
    assert text == f"created widget-uploads: {status}"
    assert cls.split() == ["message", "message-warning"]


def test_create_upload_lock_busy_message_renders_amber_end_to_end(client, csrf_token, monkeypatch):
    """The source row exists but is EMPTY — the operator has to come back and
    upload. That must reach them as an amber banner on the source page, not as
    a green "created" or (as before the fix) nothing at all."""
    _login(client)
    monkeypatch.setattr(admin.sources_repo, "create_source", MagicMock(return_value=42))
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=_upload_record(id=42, name="widget-uploads")))
    ingest_mock = MagicMock()
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", ingest_mock)

    assert admin._manual_sync_lock.acquire(blocking=False)
    try:
        resp = client.post(
            "/admin/sources/new",
            data={"csrf_token": csrf_token, "name": "widget-uploads", "source_type": "upload"},
            files={"files": ("a.md", b"# hello world", "text/markdown")},
            follow_redirects=True,
        )

        assert resp.status_code == 200, resp.text
        assert resp.url.path == "/admin/sources/42"
        cls, text = _rendered_banner(resp.text)
        assert text == "created widget-uploads — upload skipped: sync in progress"
        assert cls.split() == ["message", "message-warning"]
        ingest_mock.assert_not_called()
        assert admin._manual_sync_lock.locked()  # still this test's own acquire
    finally:
        admin._manual_sync_lock.release()


def test_upload_submit_success_message_renders_without_the_warning_modifier(client, csrf_token, monkeypatch):
    """Same contract on the edit page's standing upload form, whose inline
    redirect goes through the same `_level_suffix`."""
    _login(client)
    record = _upload_record(id=5)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    outcome = SourceOutcome(name="widget-uploads", status="ok", pages_fetched=1, chunks_indexed=3)
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", MagicMock(return_value=outcome))

    resp = client.post(
        "/admin/sources/5/upload",
        data={"csrf_token": csrf_token},
        files={"files": ("a.md", b"# hello world", "text/markdown")},
        follow_redirects=True,
    )

    assert resp.status_code == 200, resp.text
    assert resp.url.path == "/admin/sources/5"
    cls, text = _rendered_banner(resp.text)
    assert text == "uploaded widget-uploads: ok"
    assert cls == "message"


@pytest.mark.parametrize("status", ["partial", "failed", "some-future-status"])
def test_upload_submit_non_ok_status_renders_a_warning_banner(client, csrf_token, monkeypatch, status):
    _login(client)
    record = _upload_record(id=5)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))
    outcome = SourceOutcome(name="widget-uploads", status=status, pages_fetched=1, chunks_indexed=0)
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", MagicMock(return_value=outcome))

    resp = client.post(
        "/admin/sources/5/upload",
        data={"csrf_token": csrf_token},
        files={"files": ("a.md", b"# hello world", "text/markdown")},
        follow_redirects=True,
    )

    assert resp.status_code == 200, resp.text
    assert resp.url.path == "/admin/sources/5"
    cls, text = _rendered_banner(resp.text)
    assert text == f"uploaded widget-uploads: {status}"
    assert cls.split() == ["message", "message-warning"]


# `?level=` is fully URL-controlled — anyone can hand an operator a link with
# any value in it — and it is interpolated into a `class` attribute, so it is
# WHITELISTED (only the literal "warning" survives), not sanitized.


def test_source_page_renders_the_warning_modifier_for_the_whitelisted_level(client, monkeypatch):
    """Positive control for the whitelist tests below: without this, "the class
    is always plain `.message`" would also pass on a page that ignores `level`
    entirely."""
    _login(client)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=_upload_record(id=7)))

    resp = client.get("/admin/sources/7", params={"msg": "upload failed: nope", "level": "warning"})

    assert resp.status_code == 200
    cls, text = _rendered_banner(resp.text)
    assert text == "upload failed: nope"
    assert cls.split() == ["message", "message-warning"]


@pytest.mark.parametrize(
    "level",
    [
        "<script>evil</script>",  # injection attempt
        "danger",  # a plausible-but-unlisted severity
        "error",
        "Warning",  # mis-cased: exact match only
        "WARNING",
        " warning",  # untrimmed: exact match only
        "warning ",
        "warning danger",  # smuggling a second class in
        "message-warning",  # naming the CSS class directly
        "",  # present but empty
    ],
)
def test_source_page_ignores_a_level_outside_the_whitelist(client, monkeypatch, level):
    _login(client)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=_upload_record(id=7)))

    resp = client.get("/admin/sources/7", params={"msg": "hello operator", "level": level})

    assert resp.status_code == 200
    cls, text = _rendered_banner(resp.text)
    assert cls == "message", f"level={level!r} reached the class attribute"
    assert text == "hello operator"


def test_url_supplied_level_never_appears_in_the_response_at_all(client, monkeypatch):
    """Stronger than "the class is plain": the attacker-supplied value must not
    be echoed anywhere in the body — not raw, and not merely HTML-escaped.
    `evil()` contains no character autoescaping touches, so it would show up in
    the response under BOTH forms if the value were reflected at all."""
    _login(client)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=_upload_record(id=7)))
    payload = '"><script>evil()</script>'

    resp = client.get("/admin/sources/7", params={"msg": "hello operator", "level": payload})

    assert resp.status_code == 200
    cls, _text = _rendered_banner(resp.text)
    assert cls == "message"
    assert payload not in resp.text
    assert "evil" not in resp.text


def test_banner_message_text_itself_is_autoescaped(client, monkeypatch):
    """`msg` is URL-controlled too, and carries parser-produced text. It is
    rendered as escaped TEXT, never as markup."""
    _login(client)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=_upload_record(id=7)))

    resp = client.get("/admin/sources/7", params={"msg": "<script>evil()</script>"})

    assert resp.status_code == 200
    _cls, text = _rendered_banner(resp.text)
    assert text == "&lt;script&gt;evil()&lt;/script&gt;"
    assert "<script>evil()</script>" not in resp.text


def test_source_page_without_a_msg_param_renders_no_banner(client, monkeypatch):
    """`{% if message %}`: a plain GET of a source page must not render an
    empty banner box."""
    _login(client)
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=_upload_record(id=7)))

    resp = client.get("/admin/sources/7")

    assert resp.status_code == 200
    _assert_no_banner(resp.text)


def test_new_source_form_without_a_msg_param_renders_no_banner(client):
    _login(client)
    resp = client.get("/admin/sources/new")
    assert resp.status_code == 200
    _assert_no_banner(resp.text)


# index.html parity: `?msg=`/`?level=` must mean the same thing on both pages.


def _stub_empty_source_lists(monkeypatch) -> None:
    monkeypatch.setattr(admin.sources_repo, "list_sources", lambda conn, *, status=None: [])


def test_index_renders_the_warning_modifier_for_the_whitelisted_level(client, monkeypatch):
    _login(client)
    _stub_empty_source_lists(monkeypatch)

    resp = client.get("/admin", params={"msg": "created widget — upload failed: nope", "level": "warning"})

    assert resp.status_code == 200
    cls, text = _rendered_banner(resp.text)
    assert text == "created widget — upload failed: nope"
    assert cls.split() == ["message", "message-warning"]


@pytest.mark.parametrize("level", ["<script>evil</script>", "danger", "Warning", "message-warning", ""])
def test_index_ignores_a_level_outside_the_whitelist(client, monkeypatch, level):
    _login(client)
    _stub_empty_source_lists(monkeypatch)

    resp = client.get("/admin", params={"msg": "created widget", "level": level})

    assert resp.status_code == 200
    cls, text = _rendered_banner(resp.text)
    assert cls == "message", f"level={level!r} reached the class attribute"
    assert text == "created widget"


def test_index_success_message_renders_without_the_warning_modifier(client, monkeypatch):
    """The crawl-create redirect (`/admin?msg=created+{name}`, no `level`) is
    the common green case on this page."""
    _login(client)
    _stub_empty_source_lists(monkeypatch)

    resp = client.get("/admin", params={"msg": "created widget"})

    assert resp.status_code == 200
    cls, text = _rendered_banner(resp.text)
    assert cls == "message"
    assert text == "created widget"


def test_index_without_a_msg_param_renders_no_banner(client, monkeypatch):
    _login(client)
    _stub_empty_source_lists(monkeypatch)

    resp = client.get("/admin")

    assert resp.status_code == 200
    _assert_no_banner(resp.text)


# (i) CSRF on the MULTIPART create path -------------------------------------------------------
#
# `require_csrf` reads `csrf_token` via `Form(default="")`. The parametrized
# rejection test near the top of this file posts a urlencoded body; the create
# form is now multipart, so the rejection direction has to be proven for a
# multipart body too — a `Form` parameter that failed to resolve from multipart
# would 422 (or, worse, silently read as absent-but-valid) instead of 403.


def test_create_multipart_without_csrf_token_is_rejected(client, monkeypatch):
    _login(client)
    create_mock = MagicMock(return_value=42)
    monkeypatch.setattr(admin.sources_repo, "create_source", create_mock)
    ingest_mock = MagicMock()
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", ingest_mock)

    resp = client.post(
        "/admin/sources/new",
        data={"name": "widget-uploads", "source_type": "upload"},
        files={"files": ("a.md", b"# hello world", "text/markdown")},
        follow_redirects=False,
    )

    assert resp.status_code == 403, resp.text  # not 422, and emphatically not 303
    create_mock.assert_not_called()
    ingest_mock.assert_not_called()
    assert not admin._manual_sync_lock.locked()


def test_create_multipart_with_wrong_csrf_token_is_rejected(client, monkeypatch):
    _login(client)
    create_mock = MagicMock(return_value=42)
    monkeypatch.setattr(admin.sources_repo, "create_source", create_mock)

    resp = client.post(
        "/admin/sources/new",
        data={"csrf_token": "not-the-right-value", "name": "widget-uploads", "source_type": "upload"},
        files={"files": ("a.md", b"# hello world", "text/markdown")},
        follow_redirects=False,
    )

    assert resp.status_code == 403, resp.text
    create_mock.assert_not_called()


def test_create_multipart_with_browser_empty_file_part_and_no_csrf_is_rejected(client, csrf_token, monkeypatch):
    """The exact body shape a browser submits for a crawl create (an untouched
    file input) must not become a CSRF bypass: the empty part is skipped by
    `_parse_upload_files`, but the token check runs before any of that."""
    _login(client)
    create_mock = MagicMock(return_value=42)
    monkeypatch.setattr(admin.sources_repo, "create_source", create_mock)

    body, headers = _raw_multipart(
        [("name", "widget"), ("base_url", "https://widget.example.com/docs/"), ("source_type", "crawl")],
        [("files", "", b"", "application/octet-stream")],
    )
    resp = client.post("/admin/sources/new", content=body, headers=headers, follow_redirects=False)

    assert resp.status_code == 403, resp.text
    create_mock.assert_not_called()


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


def test_build_source_config_upload_synthesizes_base_url():
    """source_type='upload' must ignore whatever raw base_url string was
    passed in and synthesize the 'upload://{name}' sentinel instead."""
    cfg, error = admin._build_source_config(
        name="x",
        base_url="",
        sitemap="",
        include_prefixes="",
        exclude_prefixes="",
        max_pages="",
        language="english",
        rate_limit_rps="1.0",
        source_type="upload",
        taken=set(),
    )
    assert error is None
    assert isinstance(cfg, SourceConfig)
    assert cfg.source_type == "upload"
    assert cfg.base_url == "upload://x"


def test_build_source_config_upload_ignores_submitted_base_url():
    cfg, error = admin._build_source_config(
        name="x",
        base_url="https://evil.example.com/",
        sitemap="",
        include_prefixes="",
        exclude_prefixes="",
        max_pages="",
        language="english",
        rate_limit_rps="1.0",
        source_type="upload",
    )
    assert error is None
    assert cfg.base_url == "upload://x"


def test_record_to_config_roundtrip():
    record = _make_record(name="widget", base_url="https://widget.example.com/docs/", max_pages=10)
    cfg = admin._record_to_config(record)
    assert isinstance(cfg, SourceConfig)
    assert cfg.name == "widget"
    assert cfg.max_pages == 10


def _request_with_query(query: str) -> Request:
    """A bare ASGI GET request carrying `query`, for the `?level=` helpers —
    they only ever touch `request.query_params`."""
    return Request({"type": "http", "method": "GET", "path": "/admin", "query_string": query.encode(), "headers": []})


@pytest.mark.parametrize(
    "query,expected",
    [
        ("level=warning", "warning"),  # the ONLY accepted value
        ("", None),  # no level at all: the success default
        ("msg=created+widget", None),
        ("level=", None),
        ("level=Warning", None),  # exact match, so mis-casing is rejected
        ("level=WARNING", None),
        ("level=+warning", None),  # untrimmed
        ("level=warning+danger", None),  # class smuggling
        ("level=danger", None),
        ("level=message-warning", None),  # naming the CSS class directly
        ("level=%3Cscript%3Eevil%3C%2Fscript%3E", None),  # injection attempt
    ],
)
def test_message_level_accepts_only_the_whitelisted_literal(query, expected):
    """The return value is interpolated into a `class` attribute, so anything
    that is not an exact whitelist member collapses to None (green default)."""
    assert admin._message_level(_request_with_query(query)) == expected


@pytest.mark.parametrize(
    "status,expected",
    [
        ("ok", ""),  # the only clean success: no level param, green banner
        ("partial", "&level=warning"),
        ("failed", "&level=warning"),
        ("some-future-status", "&level=warning"),  # unknown => warn, never green
        ("OK", "&level=warning"),  # exact match: no case-insensitive success
        ("", "&level=warning"),
    ],
)
def test_level_suffix_treats_only_ok_as_success(status, expected):
    assert admin._level_suffix(status) == expected


def test_list_docs_view(client, csrf_token, monkeypatch):
    _login(client)
    page_rec = PageRecord(
        id=1,
        source_id=5,
        source_name="widget",
        url="https://widget.example.com/docs/guide",
        content_hash="abc123hash",
        fetched_at=datetime.now(UTC),
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
    # A real `SourceOutcome` (not a bare `MagicMock()`): `store.sync_source`'s
    # return value now flows into `metrics.record_sync_outcome` via
    # `store.sync_source_with_metrics`, which does real arithmetic
    # (`.inc(outcome.pages_fetched)`, etc.) on the outcome's fields — a
    # `MagicMock` return value fails that arithmetic with a `TypeError`.
    sync_mock = MagicMock(return_value=SourceOutcome(name="src", status="ok"))
    monkeypatch.setattr(admin.store, "sync_source", sync_mock)

    resp = client.post("/admin/sync-all", data={"csrf_token": csrf_token}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin?msg=full_sync_completed"
    assert sync_mock.call_count == 2


def test_sync_all_submit_skips_upload_sources_cleanly(client, csrf_token, monkeypatch):
    """T19_FIX: `_bg_sync_all` (and `sync_all_submit`'s own synchronous
    pytest-branch) must filter out `source_type='upload'` records BEFORE
    building any `SourceConfig` -- previously a single upload source
    anywhere in the active set made `_record_to_config` raise, aborting the
    entire full sync (crawl sources included). A mix of crawl + upload
    sources must still sync every crawl source and skip the upload source
    without error."""
    _login(client)
    active_sources = [
        _make_record(id=1, name="src-a", status="active"),
        _make_record(id=2, name="widget-uploads", status="active", source_type="upload", base_url="upload://widget-uploads"),
        _make_record(id=3, name="src-b", status="active"),
    ]
    monkeypatch.setattr(admin.sources_repo, "list_sources", MagicMock(return_value=active_sources))
    sync_mock = MagicMock(return_value=SourceOutcome(name="src", status="ok"))
    monkeypatch.setattr(admin.store, "sync_source", sync_mock)
    ingest_mock = MagicMock()
    monkeypatch.setattr(admin.store, "ingest_uploaded_docs", ingest_mock)

    resp = client.post("/admin/sync-all", data={"csrf_token": csrf_token}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin?msg=full_sync_completed"
    # Only the two crawl sources were synced -- the upload source was
    # skipped, not attempted and not errored on.
    assert sync_mock.call_count == 2
    synced_names = {call.args[0].name for call in sync_mock.call_args_list}
    assert synced_names == {"src-a", "src-b"}
    ingest_mock.assert_not_called()
    assert not admin._manual_sync_lock.locked()


def test_bg_sync_all_filters_upload_sources_before_building_configs(monkeypatch):
    """Unit-level check on `_bg_sync_all` itself (called directly, the way
    `test_sync_health.py` already does, and the way the real background
    path -- `run_sync_task(_bg_sync_all, ...)` -- calls it outside pytest):
    a `source_type='upload'` record must never reach `_record_to_config`."""
    sources = [
        _make_record(id=1, name="src-a", status="active"),
        _make_record(id=2, name="widget-uploads", status="active", source_type="upload", base_url="upload://widget-uploads"),
    ]
    record_to_config_spy = MagicMock(wraps=admin._record_to_config)
    monkeypatch.setattr(admin, "_record_to_config", record_to_config_spy)
    sync_all_mock = MagicMock(return_value={"src-a": SourceOutcome(name="src-a", status="ok")})
    monkeypatch.setattr(admin.store, "sync_all", sync_all_mock)
    # Force the non-pytest branch (real `store.sync_all` call path) even
    # though this runs under pytest.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("SYNC_RUNNER_SYNC", "0")

    admin._bg_sync_all(sources, conn_factory=lambda: object())

    record_to_config_spy.assert_called_once()
    assert record_to_config_spy.call_args.args[0].name == "src-a"
    sync_all_mock.assert_called_once()
    (cfgs_arg,) = sync_all_mock.call_args.args
    assert [c.name for c in cfgs_arg] == ["src-a"]


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
    now = datetime.now(UTC)
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


def test_admin_purge_source_submit(client, csrf_token, monkeypatch):
    _login(client)
    record = _make_record(id=10, name="widget", status="active")
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))

    purged_id = []
    def fake_purge(conn, source_id):
        purged_id.append(source_id)
        return 42

    monkeypatch.setattr(admin.store, "purge_source", fake_purge)

    resp = client.post("/admin/sources/10/purge", data={"csrf_token": csrf_token}, follow_redirects=False)
    assert resp.status_code == 303
    assert "purged+widget" in resp.headers["location"]
    assert purged_id == [10]


def test_admin_refresh_source_submit(client, csrf_token, monkeypatch):
    _login(client)
    record = _make_record(id=11, name="widget-refresh", status="active")
    monkeypatch.setattr(admin.sources_repo, "get_source", MagicMock(return_value=record))

    purged_id = []
    def fake_purge(conn, source_id):
        purged_id.append(source_id)
        return 42

    monkeypatch.setattr(admin.store, "purge_source", fake_purge)

    outcome = SourceOutcome(name="widget-refresh", status="ok", pages_fetched=5, chunks_indexed=20)
    sync_mock = MagicMock(return_value=outcome)
    monkeypatch.setattr(admin.store, "sync_source", sync_mock)

    resp = client.post("/admin/sources/11/refresh", data={"csrf_token": csrf_token}, follow_redirects=False)
    assert resp.status_code == 303
    assert "refreshed+widget-refresh:+ok" in resp.headers["location"]
    assert purged_id == [11]
    sync_mock.assert_called_once()


def test_admin_stop_sync_submit(client, csrf_token):
    _login(client)
    admin._sync_cancel_event.clear()
    resp = client.post("/admin/sync/stop", data={"csrf_token": csrf_token}, follow_redirects=False)
    assert resp.status_code == 303
    assert "stop_triggered" in resp.headers["location"]
    assert admin._sync_cancel_event.is_set()
