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

import asyncio
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("MCP_TOKEN", "test-mcp-token-for-server-tests")

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

import app.server as server

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
