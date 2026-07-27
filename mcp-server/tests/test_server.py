"""Tests for app.server: tool wrappers, MCP_TOKEN bearer auth on /mcp, and the
unauthenticated /metrics regression guard.

`app.server` reads `MCP_TOKEN` (required) at import time and refuses to start
(SystemExit) if it is unset/empty, mirroring ingestion/app/main.py:36-41
(SYNC_TOKEN). A test token is exported *before* the first `import app.server`
below so the existing tool-wrapper tests keep working under the documented
run command, which does not itself set MCP_TOKEN:

    POSTGRES_PASSWORD=change-me ./.venv/bin/pytest -q

The startup-failure case (no/empty MCP_TOKEN) is exercised in a subprocess
since the guard only fires once, at first import, in this process.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import os
import socket
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

os.environ.setdefault("MCP_TOKEN", "test-mcp-token-for-server-tests")

import app.server as server
import httpx
import psycopg
import pytest
from app import retrieval, source_defaults
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.auth import AccessToken

TEST_TOKEN = os.environ["MCP_TOKEN"]


def _server_root() -> Path:
    return Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Tool wrapper tests (unchanged behaviour)
# ---------------------------------------------------------------------------


def test_search_docs_clamps_limit_above_max(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server.retrieval,
        "search",
        lambda query, source=None, limit=5: calls.append(limit) or "ok",
    )
    server.search_docs("query", limit=999)
    assert calls == [20]


def test_search_docs_clamps_limit_below_min(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server.retrieval,
        "search",
        lambda query, source=None, limit=5: calls.append(limit) or "ok",
    )
    server.search_docs("query", limit=-5)
    assert calls == [1]

    calls.clear()
    server.search_docs("query", limit=0)
    assert calls == [1]


def test_search_docs_passes_through_valid_limit(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server.retrieval,
        "search",
        lambda query, source=None, limit=5: calls.append(limit) or "ok",
    )
    server.search_docs("query", limit=7)
    assert calls == [7]


# ---------------------------------------------------------------------------
# MCP_TOKEN startup guard
# ---------------------------------------------------------------------------


def test_missing_mcp_token_refuses_to_start():
    """Importing app.server without MCP_TOKEN set must exit non-zero before
    the ASGI app (and therefore any socket) is ever created."""
    env = os.environ.copy()
    env.pop("MCP_TOKEN", None)

    proc = subprocess.run(
        [sys.executable, "-c", "import app.server"],
        cwd=str(_server_root()),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "MCP_TOKEN" in proc.stderr


def test_empty_mcp_token_refuses_to_start():
    """An empty string is falsy — same fail-fast path as unset."""
    env = os.environ.copy()
    env["MCP_TOKEN"] = ""

    proc = subprocess.run(
        [sys.executable, "-c", "import app.server"],
        cwd=str(_server_root()),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "MCP_TOKEN" in proc.stderr


# ---------------------------------------------------------------------------
# /mcp bearer auth + /metrics regression guard
#
# Requests are driven in-process against the real streamable-HTTP ASGI app
# (server.mcp.http_app(...)) via httpx.ASGITransport — no real socket is
# bound. The 401 cases are rejected by RequireAuthMiddleware before the
# request ever reaches the MCP session machinery, so no app lifespan is
# needed for those. The "correct token" case exercises a real tool call
# through the MCP protocol (fastmcp.Client), which requires the app's
# lifespan (it starts the StreamableHTTPSessionManager) to be active.
# ---------------------------------------------------------------------------


def _mcp_asgi_app():
    return server.mcp.http_app(transport="http", stateless_http=True)


def _post_mcp(app, headers):
    async def _do():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            return await client.post("/mcp", json=body, headers=headers)

    return asyncio.run(_do())


_MCP_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


def test_mcp_without_authorization_header_is_401():
    resp = _post_mcp(_mcp_asgi_app(), _MCP_HEADERS)
    assert resp.status_code == 401


def test_mcp_with_wrong_token_is_401():
    headers = {**_MCP_HEADERS, "authorization": "Bearer wrong-token"}
    resp = _post_mcp(_mcp_asgi_app(), headers)
    assert resp.status_code == 401


def test_mcp_with_non_ascii_token_is_401_not_500():
    """Regression guard: hmac.compare_digest on two `str` operands raises
    TypeError for non-ASCII bytes, which previously propagated out of
    verify_token uncaught (500 + traceback in logs, triggerable by any
    unauthenticated client with one byte). BearerTokenVerifier must encode
    both operands to bytes before comparing so this fails closed as a
    regular 401, not an unhandled exception."""
    # httpx enforces ASCII-only str header values; a raw non-ASCII header byte
    # (as an attacker would send over the wire) must be passed as bytes here to
    # bypass that client-side guard and actually reach the server's parsing.
    headers = {**_MCP_HEADERS, "authorization": "Bearer tést-token".encode("latin-1")}
    resp = _post_mcp(_mcp_asgi_app(), headers)
    assert resp.status_code == 401


def test_mcp_with_correct_token_allows_tools_list_and_search_docs(monkeypatch):
    monkeypatch.setattr(
        server.retrieval,
        "search",
        lambda query, source=None, limit=5: "formatted search result",
    )

    async def _run():
        app = _mcp_asgi_app()
        async with app.router.lifespan_context(app):

            def httpx_client_factory(
                *, headers=None, auth=None, follow_redirects=True, timeout=None, **_
            ):
                return httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                    headers=headers,
                    follow_redirects=follow_redirects,
                )

            transport = StreamableHttpTransport(
                url="http://testserver/mcp",
                headers={"Authorization": f"Bearer {TEST_TOKEN}"},
                httpx_client_factory=httpx_client_factory,
            )
            client = Client(transport)
            async with client:
                tools = await client.list_tools()
                result = await client.call_tool(
                    "search_docs", {"query": "test", "limit": 1}
                )
            return tools, result

    tools, result = asyncio.run(_run())

    tool_names = {t.name for t in tools}
    assert {"search_docs", "list_doc_sources"} <= tool_names
    assert result.data == "formatted search result"


# ---------------------------------------------------------------------------
# propose_doc_source: no live Postgres in this test run (pytest cannot reach
# the db service — see mcp-server/tests/test_retrieval_integration.py's
# module docstring for why). Exercised here against a fake pool that plays
# the same `with pool.connection() as conn: with conn.cursor() as cur: ...`
# protocol as psycopg_pool.ConnectionPool, so `retrieval.propose_source`'s
# real validation/SQL-building/error-handling logic runs end-to-end without
# a database. DB-touching behavior itself (actual INSERT, the real UNIQUE
# constraint) is verified separately — see this task's Result block.
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, on_execute):
        self._on_execute = on_execute
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self._row = self._on_execute(sql, params)

    def fetchone(self):
        return self._row


class _FakeConnection:
    def __init__(self, on_execute):
        self._on_execute = on_execute

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # Mirrors psycopg's real `with conn:` transaction context: propagate
        # (don't swallow) any exception raised inside the block, exactly as
        # the real pool.connection()'s `with conn:` would before rollback.
        return False

    def cursor(self):
        return _FakeCursor(self._on_execute)


class _FakePool:
    def __init__(self, on_execute):
        self._on_execute = on_execute

    @contextmanager
    def connection(self):
        yield _FakeConnection(self._on_execute)


def _install_fake_pool(monkeypatch, on_execute):
    monkeypatch.setattr(retrieval, "get_pool", lambda: _FakePool(on_execute))


def _valid_kwargs(**overrides):
    kwargs = dict(
        name="my-new-source",
        base_url="https://docs.python.org/docs/",
        max_pages=100,
        sitemap=None,
        include_prefixes=["/docs/"],
        exclude_prefixes=[],
        language="english",
        rate_limit_rps=1.0,
        proposed_by_token="super-secret-mcp-token",
    )
    kwargs.update(overrides)
    return kwargs


def test_propose_source_valid_inserts_exactly_one_pending_row(monkeypatch):
    calls = []

    def on_execute(sql, params):
        calls.append((sql, params))
        return {"id": 42}

    _install_fake_pool(monkeypatch, on_execute)

    source_id = retrieval.propose_source(**_valid_kwargs())

    assert source_id == 42
    assert len(calls) == 1
    sql, params = calls[0]
    # status is a hardcoded SQL literal, never a bind parameter.
    assert "'pending'" in sql
    assert "status" not in params
    assert params["name"] == "my-new-source"


def test_propose_source_omitted_max_pages_gets_the_derived_default(monkeypatch):
    """CRITICAL-1/2 fix: `max_pages` has no supported "unlimited" opt-out.
    Because `max_pages: int | None = None` makes an explicit `max_pages=None`
    indistinguishable from omitting the argument, BOTH now always receive
    `apply_creation_defaults`'s `DEFAULT_MAX_PAGES` ceiling — even for a
    NAMED proposal (derivation is no longer gated on `name is None`). This
    replaces the old (unsafe) expectation that `max_pages=None` was written
    through as NULL/no-ceiling."""
    calls = []

    def on_execute(sql, params):
        calls.append((sql, params))
        return {"id": 7}

    _install_fake_pool(monkeypatch, on_execute)

    source_id = retrieval.propose_source(**_valid_kwargs(max_pages=None))
    assert source_id == 7
    assert len(calls) == 1
    _, params = calls[0]
    assert params["max_pages"] == source_defaults.DEFAULT_MAX_PAGES


def test_propose_source_invalid_config_inserts_nothing(monkeypatch):
    calls = []
    _install_fake_pool(monkeypatch, lambda sql, params: calls.append((sql, params)))

    with pytest.raises(retrieval.ProposalError, match="invalid source configuration"):
        retrieval.propose_source(**_valid_kwargs(name="Not Valid! Name"))

    assert calls == []


def test_propose_source_rejects_own_prefix_filter_bfs_seed_bug(monkeypatch):
    """Same class of bug the T12 fix guarded against for nextjs: a
    sitemap-less base_url excluded by its own prefixes would crawl 0 pages."""
    calls = []
    _install_fake_pool(monkeypatch, lambda sql, params: calls.append((sql, params)))

    with pytest.raises(retrieval.ProposalError, match="invalid source configuration"):
        retrieval.propose_source(
            **_valid_kwargs(
                base_url="https://example.com/blog/",
                include_prefixes=["/docs/"],
                exclude_prefixes=[],
            )
        )
    assert calls == []


def test_propose_source_duplicate_name_is_clean_error_not_500(monkeypatch):
    def on_execute(sql, params):
        raise psycopg.errors.UniqueViolation(
            'duplicate key value violates unique constraint "doc_sources_name_key"'
        )

    _install_fake_pool(monkeypatch, on_execute)

    with pytest.raises(retrieval.ProposalError, match="already exists"):
        retrieval.propose_source(**_valid_kwargs())


def test_propose_source_cannot_create_active_source_no_status_kwarg():
    """There is no `status` parameter at all — attempting to pass one is a
    TypeError, not a way to sneak `status='active'` past validation."""
    kwargs = _valid_kwargs()
    kwargs["status"] = "active"
    with pytest.raises(TypeError):
        retrieval.propose_source(**kwargs)


@pytest.mark.parametrize(
    "injection_value",
    [
        "'; UPDATE doc_sources SET status='active' WHERE name='my-new-source'; --",
        "active', status='active",
        "my-new-source', 'active",
    ],
)
def test_propose_source_status_injection_via_field_values_is_rejected(monkeypatch, injection_value):
    """Try to smuggle an 'active' status through ordinary string fields
    (name/language) instead of a dedicated kwarg. Parameterized SQL plus
    ProposedSourceConfig's `name` pattern must reject every one of these
    without ever reaching the database."""
    calls = []
    _install_fake_pool(monkeypatch, lambda sql, params: calls.append((sql, params)))

    with pytest.raises(retrieval.ProposalError, match="invalid source configuration"):
        retrieval.propose_source(**_valid_kwargs(name=injection_value))

    assert calls == []


def test_propose_source_rejects_sitemap_host_mismatch(monkeypatch):
    """H1: sitemap is fetched with no independent host check, so a proposal
    with a benign-looking base_url but a sitemap on a different host must be
    rejected before anything is written — otherwise an approving human sees
    only base_url and the crawler later fetches an unrelated (possibly
    internal) host via sitemap."""
    calls = []
    _install_fake_pool(monkeypatch, lambda sql, params: calls.append((sql, params)))

    with pytest.raises(retrieval.ProposalError, match="invalid source configuration"):
        retrieval.propose_source(
            **_valid_kwargs(
                base_url="https://example.com/docs/",
                sitemap="https://internal.other-host.example/sitemap.xml",
            )
        )
    assert calls == []


def test_propose_source_rejects_private_ip_literal_base_url(monkeypatch):
    """H1: a base_url whose host is a private-space literal must be rejected
    at proposal time, never reaching the pending queue for a human to
    adjudicate."""
    calls = []
    _install_fake_pool(monkeypatch, lambda sql, params: calls.append((sql, params)))

    with pytest.raises(retrieval.ProposalError, match="invalid source configuration"):
        retrieval.propose_source(**_valid_kwargs(base_url="http://192.168.1.1/docs/"))
    assert calls == []


def test_propose_source_rejects_decimal_encoded_loopback_base_url(monkeypatch):
    """H1: decimal-encoded IP literals (e.g. 2130706433 == 127.0.0.1) must not
    slip past the private-address check — pydantic's HttpUrl/getaddrinfo
    normalize the encoding before ipaddress inspects it."""
    calls = []
    _install_fake_pool(monkeypatch, lambda sql, params: calls.append((sql, params)))

    with pytest.raises(retrieval.ProposalError, match="invalid source configuration"):
        retrieval.propose_source(**_valid_kwargs(base_url="http://2130706433/docs/"))
    assert calls == []


def test_propose_source_rejects_public_hostname_resolving_to_private_address(monkeypatch):
    """H1: a hostname that looks public but resolves (e.g. via attacker-
    controlled DNS) to a private address must also be rejected — the check
    is on the resolved address, not just the literal host string."""
    calls = []
    _install_fake_pool(monkeypatch, lambda sql, params: calls.append((sql, params)))

    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, *args, **kwargs):
        if host == "looks-public-but-isnt.example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(retrieval.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(retrieval.ProposalError, match="invalid source configuration"):
        retrieval.propose_source(
            **_valid_kwargs(base_url="https://looks-public-but-isnt.example.com/docs/")
        )
    assert calls == []


def test_propose_source_fails_closed_when_dns_resolution_fails(monkeypatch):
    """H1: an unresolvable host must be rejected (fail closed), not treated
    as safe-by-default — it isn't fetchable anyway, so there is no usability
    cost to refusing it."""
    calls = []
    _install_fake_pool(monkeypatch, lambda sql, params: calls.append((sql, params)))

    def fake_getaddrinfo(host, *args, **kwargs):
        raise socket.gaierror("name resolution failed")

    monkeypatch.setattr(retrieval.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(retrieval.ProposalError, match="invalid source configuration"):
        retrieval.propose_source(
            **_valid_kwargs(base_url="https://this-host-does-not-resolve.invalid/docs/")
        )
    assert calls == []


# ---------------------------------------------------------------------------
# T13: placeholder/reserved documentation-example hosts. A live-database
# audit found test/placeholder rows in the production index, including a
# page indexed under a trusted source name at
# 'https://docs.company.com/api-reference'. Reject RFC 2606/6761
# reserved/placeholder hosts at proposal time so this class of row can never
# be re-proposed. Mirrors ingestion/tests/test_config.py's coverage of
# SourceConfig's identical validator.
# ---------------------------------------------------------------------------


def _make_public_getaddrinfo(hosts: tuple[str, ...]):
    """Real `socket.getaddrinfo`, except the given hostnames are stubbed to
    resolve to a fixed public, non-reserved IPv4 literal — needed because
    ProposedSourceConfig's `_hosts_must_not_be_private` fails CLOSED on an
    unresolvable host, and several of these fixture hostnames (foo.test,
    foo.invalid, foo.example, docs.company.com, widget.example.com) are not
    real, resolvable domains. Without this, those cases would be rejected by
    the private-host check before ever reaching the placeholder check this
    test targets."""
    real_getaddrinfo = socket.getaddrinfo

    def fake(host, *args, **kwargs):
        if host in hosts:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        return real_getaddrinfo(host, *args, **kwargs)

    return fake


# example.com/.org/.net are real, publicly resolvable RFC 2606 domains, so no
# DNS stub is needed for them; the others are not real domains and need one.
_UNRESOLVABLE_PLACEHOLDER_HOSTS = ("docs.company.com", "foo.test", "foo.invalid", "foo.example")


@pytest.mark.parametrize(
    "base_url",
    [
        "https://example.com/docs/",
        "https://example.org/docs/",
        "https://example.net/docs/",
        "https://docs.company.com/docs/",
        "https://foo.test/docs/",
        "https://foo.invalid/docs/",
        "https://foo.example/docs/",
    ],
)
def test_propose_source_rejects_placeholder_base_url(monkeypatch, base_url):
    calls = []
    _install_fake_pool(monkeypatch, lambda sql, params: calls.append((sql, params)))
    monkeypatch.setattr(
        retrieval.socket, "getaddrinfo", _make_public_getaddrinfo(_UNRESOLVABLE_PLACEHOLDER_HOSTS)
    )

    with pytest.raises(retrieval.ProposalError) as e:
        retrieval.propose_source(**_valid_kwargs(base_url=base_url))
    assert "reserved documentation-example host" in str(e.value)
    assert calls == []


@pytest.mark.parametrize("base_url", ["https://localhost/docs/", "https://foo.localhost/docs/"])
def test_propose_source_rejects_loopback_placeholder_base_url(monkeypatch, base_url):
    """localhost / *.localhost are also placeholder hosts, but resolve to
    loopback for real, so `_hosts_must_not_be_private` (declared earlier in
    ProposedSourceConfig, and therefore runs first) rejects them before the
    placeholder validator gets a chance to. Still rejected, just for a
    different, earlier-in-the-chain reason — mirrors ingestion's
    test_loopback_placeholder_base_url_is_rejected."""
    calls = []
    _install_fake_pool(monkeypatch, lambda sql, params: calls.append((sql, params)))

    with pytest.raises(retrieval.ProposalError, match="invalid source configuration"):
        retrieval.propose_source(**_valid_kwargs(base_url=base_url))
    assert calls == []


def test_propose_source_accepts_subdomain_of_placeholder_host(monkeypatch):
    """Only exact placeholder hosts / their reserved suffixes are rejected —
    a real subdomain like widget.example.com must keep working (given a DNS
    stub, since widget.example.com isn't a real resolvable domain)."""
    calls = []

    def on_execute(sql, params):
        calls.append((sql, params))
        return {"id": 42}

    _install_fake_pool(monkeypatch, on_execute)
    monkeypatch.setattr(
        retrieval.socket, "getaddrinfo", _make_public_getaddrinfo(("widget.example.com",))
    )

    source_id = retrieval.propose_source(**_valid_kwargs(base_url="https://widget.example.com/docs/"))
    assert source_id == 42
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# T11: propose_source(name=None) — URL-only mode. `name` is queried/derived
# via a SELECT-existing-names round-trip only when omitted, so these tests
# use a small dedicated fake pool/cursor that supports both `fetchall`
# (existing names) and `fetchone` (the INSERT's RETURNING id), unlike
# `_FakeCursor` above which only ever backs a single INSERT round-trip.
# ---------------------------------------------------------------------------


class _NameQueryFakeCursor:
    def __init__(self, existing_names, insert_row):
        self._existing_names = existing_names
        self._insert_row = insert_row
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.executed.append((sql, params))

    def fetchall(self):
        return [{"name": n} for n in self._existing_names]

    def fetchone(self):
        return self._insert_row


class _NameQueryFakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self._cursor


class _NameQueryFakePool:
    def __init__(self, cursor):
        self._cursor = cursor

    @contextmanager
    def connection(self):
        yield _NameQueryFakeConnection(self._cursor)


def test_propose_source_url_only_derives_name_prefixes_and_max_pages(monkeypatch):
    """T11 acceptance: propose_source(base_url=...) alone (name omitted)
    writes exactly one pending row with a derived name, derived
    include_prefixes, and a real (non-None) max_pages — never the
    empty-prefixes/unlimited-pages combination that would allow crawling an
    entire shared docs host (see source_defaults module docstring)."""
    cursor = _NameQueryFakeCursor(existing_names=set(), insert_row={"id": 99})
    monkeypatch.setattr(retrieval, "get_pool", lambda: _NameQueryFakePool(cursor))

    source_id = retrieval.propose_source(
        base_url="https://doc.traefik.io/traefik/",
        proposed_by_token="super-secret-mcp-token",
    )

    assert source_id == 99
    # SELECT (existing names) + INSERT — exactly two round-trips in URL-only mode.
    assert len(cursor.executed) == 2
    insert_sql, insert_params = cursor.executed[-1]
    assert "'pending'" in insert_sql
    assert insert_params["name"] == "traefik"
    assert insert_params["include_prefixes"] == ["/traefik"]
    assert insert_params["max_pages"] == source_defaults.DEFAULT_MAX_PAGES
    assert insert_params["max_pages"] is not None


def test_propose_source_url_only_avoids_name_collision(monkeypatch):
    """`taken` passed to `derive_name` must include every existing name
    (any status), so a second URL-only proposal against the same host gets
    a `-2` suffix instead of colliding."""
    cursor = _NameQueryFakeCursor(existing_names={"traefik"}, insert_row={"id": 100})
    monkeypatch.setattr(retrieval, "get_pool", lambda: _NameQueryFakePool(cursor))

    retrieval.propose_source(
        base_url="https://doc.traefik.io/traefik/",
        proposed_by_token="super-secret-mcp-token",
    )

    _, insert_params = cursor.executed[-1]
    assert insert_params["name"] == "traefik-2"


def test_propose_source_named_proposal_still_derives_prefixes_and_max_pages(monkeypatch):
    """CRITICAL-1 regression: reviewer's own reproduction. A NAMED proposal
    that omits `include_prefixes`/`max_pages` must still get derived,
    scoped `include_prefixes` and the `DEFAULT_MAX_PAGES` ceiling — the
    derivation must not be gated on `name is None`. Before the fix, this
    produced `include_prefixes == []` and `max_pages is None` (crawl the
    entire host, uncapped)."""
    cursor = _NameQueryFakeCursor(existing_names=set(), insert_row={"id": 101})
    monkeypatch.setattr(retrieval, "get_pool", lambda: _NameQueryFakePool(cursor))

    source_id = retrieval.propose_source(
        name="traefik-hub",
        base_url="https://doc.traefik.io/traefik-hub/",
        proposed_by_token="super-secret-mcp-token",
    )

    assert source_id == 101
    # Named proposals must NOT trigger the existing-names SELECT (name is
    # already supplied) — exactly one INSERT round-trip, same as before.
    assert len(cursor.executed) == 1
    insert_sql, insert_params = cursor.executed[-1]
    assert "'pending'" in insert_sql
    assert insert_params["name"] == "traefik-hub"
    assert insert_params["include_prefixes"] == ["/traefik-hub"]
    assert insert_params["max_pages"] == source_defaults.DEFAULT_MAX_PAGES
    assert insert_params["max_pages"] is not None


def test_propose_source_explicit_name_skips_the_select_round_trip(monkeypatch):
    """Regression guard: supplying an explicit `name` must not trigger the
    existing-names SELECT at all — every pre-T11 call path (explicit name)
    keeps making exactly one INSERT round-trip."""
    calls = []

    def on_execute(sql, params):
        calls.append((sql, params))
        return {"id": 1}

    _install_fake_pool(monkeypatch, on_execute)

    retrieval.propose_source(**_valid_kwargs())

    assert len(calls) == 1


def test_propose_doc_source_tool_url_only_still_derives_via_propose_source(monkeypatch):
    """The MCP tool itself accepts a bare base_url (name now optional) and
    forwards name=None through to retrieval.propose_source, which does the
    actual derivation — this only re-checks the tool's own signature/plumbing,
    not the derivation logic itself (covered above)."""
    monkeypatch.setattr(
        server,
        "get_access_token",
        lambda: AccessToken(token="a-real-mcp-token", client_id="c", scopes=[]),
    )

    captured = {}

    def fake_propose_source(**kwargs):
        captured.update(kwargs)
        return 55

    monkeypatch.setattr(server.retrieval, "propose_source", fake_propose_source)

    result = server.propose_doc_source(base_url="https://doc.traefik.io/traefik/")

    assert captured["name"] is None
    assert captured["base_url"] == "https://doc.traefik.io/traefik/"
    assert "pending" in result.lower()
    assert "auto-derived" in result.lower()


def _load_ingestion_source_config():
    """Load ingestion/app/config.py's `SourceConfig` directly from disk via
    importlib, bypassing the fact that mcp-server cannot *import* the
    ingestion package at runtime (separate venv/build context — see the
    module note above `ProposedSourceConfig` in app/retrieval.py). A test
    runner can reach the file on disk even though the two services can't
    reach each other's packages, which is exactly what the security review's
    parity-test condition asked for.

    config.py does `from .urlscope import url_host_is_private` (a relative
    import), so it must be loaded as a submodule of a real package rather
    than a bare standalone module — a synthetic package pointed at
    ingestion/app/ is registered under a name that cannot collide with
    mcp-server's own `app` package (already bound in sys.modules by this
    test file's `import app.server`).
    """
    ingestion_app_dir = Path(__file__).resolve().parents[2] / "ingestion" / "app"
    config_path = ingestion_app_dir / "config.py"
    assert config_path.is_file(), f"expected ingestion config at {config_path}"

    package_name = "_ingestion_app_for_parity_test"
    if package_name not in sys.modules:
        pkg_spec = importlib.util.spec_from_file_location(
            package_name,
            ingestion_app_dir / "__init__.py",
            submodule_search_locations=[str(ingestion_app_dir)],
        )
        package = importlib.util.module_from_spec(pkg_spec)
        sys.modules[package_name] = package
        pkg_spec.loader.exec_module(package)

    module_name = f"{package_name}.config"
    if module_name not in sys.modules:
        config_spec = importlib.util.spec_from_file_location(module_name, config_path)
        config_module = importlib.util.module_from_spec(config_spec)
        config_module.__package__ = package_name
        sys.modules[module_name] = config_module
        config_spec.loader.exec_module(config_module)
    return sys.modules[module_name].SourceConfig


def test_proposed_source_config_stays_in_sync_with_ingestion_source_config():
    """Security-review condition: convert the KEEP-THESE-TWO-CLASSES-IN-SYNC
    comment into a red test. Real cross-import of ingestion/app/config.py
    from disk (the test runner can reach it even though the two services
    cannot import each other at runtime) — compares field sets plus each
    field's annotation and constraint metadata, so a change landed in only
    one of the two models (e.g. a future H-severity fix like the sitemap-host
    / private-address checks added in this task) fails this test instead of
    silently drifting."""
    IngestionSourceConfig = _load_ingestion_source_config()

    proposed_fields = retrieval.ProposedSourceConfig.model_fields
    ingestion_fields = IngestionSourceConfig.model_fields

    assert proposed_fields.keys() == ingestion_fields.keys()

    for field_name in proposed_fields:
        p_field = proposed_fields[field_name]
        i_field = ingestion_fields[field_name]
        assert p_field.annotation == i_field.annotation, (
            f"field {field_name!r} annotation drifted: "
            f"ProposedSourceConfig={p_field.annotation!r} vs SourceConfig={i_field.annotation!r}"
        )
        # pydantic's per-constraint metadata objects (e.g. the pattern/gt
        # wrappers from `annotated_types`) don't implement value equality —
        # two `Field(pattern=...)` calls with an identical pattern produce
        # unequal-by-identity objects even within the *same* class. Compare
        # by repr (which does include every constraint value) instead of `==`.
        assert repr(p_field.metadata) == repr(i_field.metadata), (
            f"field {field_name!r} constraint metadata drifted: "
            f"ProposedSourceConfig={p_field.metadata!r} vs SourceConfig={i_field.metadata!r}"
        )
        assert p_field.default == i_field.default, (
            f"field {field_name!r} default drifted: "
            f"ProposedSourceConfig={p_field.default!r} vs SourceConfig={i_field.default!r}"
        )


def _load_ingestion_source_defaults():
    """Load ingestion/app/source_defaults.py directly from disk via the same
    importlib cross-import trick as `_load_ingestion_source_config` above
    (mcp-server cannot import the ingestion package at runtime, but a test
    runner can still reach the file on disk). Registered under the same
    synthetic `_ingestion_app_for_parity_test` package so `source_defaults`'s
    own `from __future__ import annotations`-only, import-free body loads
    without needing `config.py`'s relative import of `urlscope`."""
    ingestion_app_dir = Path(__file__).resolve().parents[2] / "ingestion" / "app"
    source_defaults_path = ingestion_app_dir / "source_defaults.py"
    assert source_defaults_path.is_file(), f"expected ingestion source_defaults at {source_defaults_path}"

    package_name = "_ingestion_app_for_parity_test"
    if package_name not in sys.modules:
        pkg_spec = importlib.util.spec_from_file_location(
            package_name,
            ingestion_app_dir / "__init__.py",
            submodule_search_locations=[str(ingestion_app_dir)],
        )
        package = importlib.util.module_from_spec(pkg_spec)
        sys.modules[package_name] = package
        pkg_spec.loader.exec_module(package)

    module_name = f"{package_name}.source_defaults"
    if module_name not in sys.modules:
        mod_spec = importlib.util.spec_from_file_location(module_name, source_defaults_path)
        mod = importlib.util.module_from_spec(mod_spec)
        mod.__package__ = package_name
        sys.modules[module_name] = mod
        mod_spec.loader.exec_module(mod)
    return sys.modules[module_name]


# Shared fixture table of URLs (mirrors ingestion/tests/test_source_defaults.py's
# own derive_name cases) exercised against BOTH copies of derive_name below —
# the drift guard for the deliberate ingestion/mcp-server duplication.
_DERIVE_NAME_FIXTURE_URLS = (
    "https://doc.traefik.io/traefik/",
    "https://developers.google.com/maps/documentation",
    "https://fastapi.tiangolo.com/",
    "https://docs.docker.com/compose/",
    "https://api.example.com/v2/widgets",
)


def _source_defaults_body_after_docstring(path: Path) -> str:
    """Return the source text of `path` with its leading module docstring
    (if any) stripped, using `ast` (not a fixed line count) so the
    comparison is robust to the docstrings themselves legitimately differing
    in wording (mcp-server's carries an extra MIRROR NOTE paragraph) while
    everything that actually executes must not."""
    src = path.read_text()
    tree = ast.parse(src)
    body = tree.body
    has_docstring = (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    )
    if has_docstring:
        start_line = body[1].lineno if len(body) > 1 else len(src.splitlines()) + 1
    else:
        start_line = body[0].lineno if body else 1
    lines = src.splitlines(keepends=True)
    return "".join(lines[start_line - 1 :])


def test_source_defaults_is_byte_identical_to_ingestion_copy_below_docstring():
    """WARNING-7 fix: the primary drift guard. Rather than exercising only
    `derive_name` over a handful of URLs (which would miss a change to
    `derive_include_prefixes`, `apply_creation_defaults`'s absent-vs-empty-
    vs-None blank detection, or `DEFAULT_MAX_PAGES` landing in only one of
    the two deliberately-duplicated `source_defaults.py` copies — see the
    MIRROR NOTE atop mcp-server/app/source_defaults.py), assert the two
    files are identical for everything that actually executes (module
    docstrings, which are allowed to differ in wording, are stripped via
    `ast` before comparing)."""
    mcp_path = Path(__file__).resolve().parents[1] / "app" / "source_defaults.py"
    ingestion_path = Path(__file__).resolve().parents[2] / "ingestion" / "app" / "source_defaults.py"
    assert mcp_path.is_file(), f"expected mcp-server source_defaults at {mcp_path}"
    assert ingestion_path.is_file(), f"expected ingestion source_defaults at {ingestion_path}"

    mcp_body = _source_defaults_body_after_docstring(mcp_path)
    ingestion_body = _source_defaults_body_after_docstring(ingestion_path)

    assert mcp_body == ingestion_body, (
        "mcp-server/app/source_defaults.py has drifted from ingestion/app/source_defaults.py "
        "below their module docstrings — DEFAULT_MAX_PAGES, derive_name, derive_include_prefixes, "
        "and/or apply_creation_defaults no longer agree between the two deliberately-duplicated copies"
    )


def test_derive_name_stays_in_sync_with_ingestion_source_defaults():
    """Readable behavioural supplement to the byte-identical check above:
    drift guard for the deliberate source_defaults.py duplication
    (mcp-server cannot import ingestion at runtime — see the module note
    atop mcp-server/app/source_defaults.py): `derive_name` must produce
    identical slugs in both copies for a shared fixture table of URLs, and
    the collision-suffix behavior (against the same `taken` set) must agree
    too."""
    ingestion_source_defaults = _load_ingestion_source_defaults()

    for url in _DERIVE_NAME_FIXTURE_URLS:
        mcp_name = source_defaults.derive_name(url, set())
        ingestion_name = ingestion_source_defaults.derive_name(url, set())
        assert mcp_name == ingestion_name, (
            f"derive_name drifted for {url!r}: mcp-server={mcp_name!r} vs ingestion={ingestion_name!r}"
        )

    taken = {"traefik"}
    assert source_defaults.derive_name(
        "https://doc.traefik.io/traefik/", taken
    ) == ingestion_source_defaults.derive_name("https://doc.traefik.io/traefik/", taken)


def test_derive_include_prefixes_stays_in_sync_with_ingestion_source_defaults():
    """Behavioural supplement: `derive_include_prefixes` (the field the
    review flagged as highest-consequence alongside `max_pages`) must agree
    between the two copies for both root and non-root `base_url`s."""
    ingestion_source_defaults = _load_ingestion_source_defaults()

    for url in _DERIVE_NAME_FIXTURE_URLS:
        assert source_defaults.derive_include_prefixes(url) == ingestion_source_defaults.derive_include_prefixes(
            url
        ), f"derive_include_prefixes drifted for {url!r}"


def test_default_max_pages_stays_in_sync_with_ingestion_source_defaults():
    """Behavioural supplement: the reviewer called `DEFAULT_MAX_PAGES` the
    highest-consequence field to leave unguarded — a change to the page
    ceiling in only one copy must fail this test."""
    ingestion_source_defaults = _load_ingestion_source_defaults()
    assert source_defaults.DEFAULT_MAX_PAGES == ingestion_source_defaults.DEFAULT_MAX_PAGES


def test_apply_creation_defaults_stays_in_sync_with_ingestion_source_defaults():
    """Behavioural supplement: `apply_creation_defaults`'s absent-vs-empty-
    vs-None blank detection (the subtlety the review specifically called
    out) must agree between the two copies for every combination of
    absent/explicit-empty/explicit-None `include_prefixes`/`max_pages`."""
    ingestion_source_defaults = _load_ingestion_source_defaults()

    base_url = "https://doc.traefik.io/traefik/"
    field_variants = (
        {"base_url": base_url},
        {"base_url": base_url, "name": "custom-name"},
        {"base_url": base_url, "include_prefixes": []},
        {"base_url": base_url, "max_pages": None},
        {"base_url": base_url, "include_prefixes": [], "max_pages": None},
        {"base_url": base_url, "include_prefixes": ["/custom"], "max_pages": 42},
    )
    for fields in field_variants:
        mcp_result = source_defaults.apply_creation_defaults(dict(fields), set())
        ingestion_result = ingestion_source_defaults.apply_creation_defaults(dict(fields), set())
        assert mcp_result == ingestion_result, f"apply_creation_defaults drifted for fields={fields!r}"


def test_derive_proposed_by_never_contains_the_token(monkeypatch):
    token = "sk-live-extremely-secret-mcp-token-value"
    proposed_by = retrieval.derive_proposed_by(token)

    assert token not in proposed_by
    assert proposed_by.startswith("token-")
    assert len(proposed_by) == len("token-") + 16


def test_propose_doc_source_tool_without_auth_context_rejects_and_writes_nothing(monkeypatch):
    """Calling the plain Python function outside a real /mcp request has no
    HTTP auth context, so get_access_token() returns None — the tool must
    fail closed (no attributable proposer) rather than write anything."""
    calls = []
    _install_fake_pool(monkeypatch, lambda sql, params: calls.append((sql, params)))

    result = server.propose_doc_source(name="some-source", base_url="https://example.com/", max_pages=10)

    assert "Rejected" in result
    assert calls == []


def test_propose_doc_source_tool_success_message_says_pending_not_live(monkeypatch):
    monkeypatch.setattr(
        server,
        "get_access_token",
        lambda: AccessToken(token="a-real-mcp-token", client_id="c", scopes=[]),
    )
    monkeypatch.setattr(
        server.retrieval,
        "propose_source",
        lambda **kwargs: 7,
    )

    result = server.propose_doc_source(name="some-source", base_url="https://example.com/", max_pages=10)

    assert "pending" in result.lower()
    assert "human" in result.lower()
    assert "not" in result.lower()  # "has NOT been crawled"


def test_propose_doc_source_tool_rejects_status_kwarg():
    """The tool's own signature has no `status` parameter, so passing one
    (an agent trying to force an active source) is a TypeError at the
    Python-call boundary, and would be an unrecognized-argument error over
    MCP's tool-call JSON schema."""
    with pytest.raises(TypeError):
        server.propose_doc_source(
            name="some-source", base_url="https://example.com/", max_pages=10, status="active"
        )


# ---------------------------------------------------------------------------
# upload_doc_text tool wrapper (T11)
# ---------------------------------------------------------------------------


def test_upload_doc_text_tool_passes_args_through_and_returns_success_message(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server.retrieval,
        "upload_text",
        lambda **kwargs: calls.append(kwargs) or "Uploaded 'T' to source 's' as 1 chunk(s) (url=upload://s/t.md).",
    )

    result = server.upload_doc_text(source="s", title="T", content="hello world")

    assert calls == [{"source": "s", "title": "T", "content": "hello world"}]
    assert "Uploaded" in result
    assert "Rejected" not in result


def test_upload_doc_text_tool_converts_upload_error_to_rejected_string(monkeypatch):
    monkeypatch.setattr(
        server.retrieval,
        "upload_text",
        lambda **kwargs: (_ for _ in ()).throw(retrieval.UploadError("unknown source 's'")),
    )

    result = server.upload_doc_text(source="s", title="T", content="hello world")

    assert result == "Rejected: unknown source 's'"


def test_upload_doc_text_tool_never_raises_on_unexpected_exception(monkeypatch):
    """The tool must never throw back to the MCP client, even for a totally
    unexpected failure (e.g. the DB connection dying mid-call) — unlike
    UploadError, this is not a case retrieval.upload_text itself guards."""

    def _boom(**kwargs):
        raise RuntimeError("db connection reset by peer: password=hunter2")

    monkeypatch.setattr(server.retrieval, "upload_text", _boom)

    result = server.upload_doc_text(source="s", title="T", content="hello world")

    assert result.startswith("Rejected:")
    # The raw exception text (which could carry sensitive detail, e.g. a
    # connection string) must never reach the caller.
    assert "hunter2" not in result
    assert "RuntimeError" not in result


def test_metrics_returns_200_with_no_auth_header():
    """Regression guard: /metrics MUST stay unauthenticated. It backs the
    Dockerfile HEALTHCHECK — if this breaks, the container restart-loops."""

    async def _do():
        app = _mcp_asgi_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.get("/metrics")

    resp = asyncio.run(_do())
    assert resp.status_code == 200
    assert "authorization" not in {h.lower() for h in resp.request.headers}
