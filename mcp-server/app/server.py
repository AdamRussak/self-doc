"""FastMCP 3.x server exposing the self-docs retrieval tools over streamable
HTTP (stateless — a search tool needs no session state, and stateless mode
survives restarts/load-balancing behind Traefik).
"""

from __future__ import annotations

import hmac
import os
import sys

import structlog
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import Response

from app import retrieval

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso", key="ts"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
)
logger = structlog.get_logger(__name__).bind(service="mcp-server")

# --- Fail fast: MCP_TOKEN is mandatory --------------------------------------------------
# Mirrors ingestion/app/main.py:36-41 (SYNC_TOKEN) — refuse to start before the
# ASGI app (and therefore any socket) exists if the operator forgot to set it.
MCP_TOKEN = os.environ.get("MCP_TOKEN")
if not MCP_TOKEN:
    print(
        "FATAL: MCP_TOKEN environment variable is required but not set. Refusing to start.",
        file=sys.stderr,
    )
    raise SystemExit(1)

# Optional comma-separated list of extra Host header values to allow, for use
# only if the FastMCP default (host_origin_protection=False, i.e. disabled) is
# ever overridden — see the HostOriginGuardMiddleware note on `main()` below.
_allowed_hosts_env = os.environ.get("MCP_ALLOWED_HOSTS", "")
ALLOWED_HOSTS = [h.strip() for h in _allowed_hosts_env.split(",") if h.strip()] or None


class BearerTokenVerifier(TokenVerifier):
    """Constant-time bearer token check against MCP_TOKEN.

    Parity with the SYNC_TOKEN check in ingestion/app/main.py:85-90
    (hmac.compare_digest). We deliberately do NOT use FastMCP's built-in
    StaticTokenVerifier (fastmcp/server/auth/providers/jwt.py:592) here: its
    token lookup is a plain dict `.get()`, which is not constant-time.
    """

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token.encode("utf-8"), self._token.encode("utf-8")):
            return None
        return AccessToken(token=token, client_id="mcp-client", scopes=[])


mcp = FastMCP("self-docs", auth=BearerTokenVerifier(MCP_TOKEN))


@mcp.tool
def search_docs(query: str, source: str | None = None, limit: int = 5) -> str:
    """Search locally indexed framework/library documentation (static reference
    knowledge: API syntax, config options, examples). Use this INSTEAD of guessing
    framework syntax from memory. NOT for project state or decisions — use Mem0
    for those. `source` optionally filters to one doc set (see list_doc_sources)."""
    clamped_limit = min(max(limit, 1), 20)
    return retrieval.search(query=query, source=source, limit=clamped_limit)


@mcp.tool
def list_doc_sources() -> str:
    """List indexed documentation sets with their last-sync time."""
    return retrieval.list_sources()


@mcp.custom_route("/metrics", methods=["GET"])
async def metrics(request: Request) -> Response:
    """Prometheus scrape endpoint (search_requests_total, search_latency_seconds)."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def main() -> None:
    logger.info("mcp_server_starting", transport="http", host="0.0.0.0", port=8000)
    # stateless_http moved off the FastMCP() constructor onto run()/http_app() in
    # FastMCP 3.x (breaking change vs 2.x) — verified against fastmcp==3.4.4,
    # where run()'s **transport_kwargs pass through to run_http_async(host=...,
    # port=..., stateless_http=...) as documented in the implementation plan.
    #
    # HostOriginGuardMiddleware (fastmcp/server/http.py:225-284): verified
    # against the installed 3.4.4 that `host_origin_protection` defaults to
    # `False` (fastmcp/settings.py:323, threaded through mixins/transport.py
    # :313-316), and the guard middleware is only inserted at all when
    # `host_origin_protection is not False` (server/http.py:642). We do not
    # pass `host_origin_protection` here, so it stays at its default `False`
    # and the middleware is never mounted — Traefik-proxied requests for an
    # external hostname are NOT rejected today. `ALLOWED_HOSTS` (from the
    # optional MCP_ALLOWED_HOSTS env var) is threaded through regardless so
    # that if host/origin protection is ever turned on (e.g. via the
    # FASTMCP_SERVER_HTTP_HOST_ORIGIN_PROTECTION env var recognized by
    # fastmcp.settings), the Traefik hostname can be allow-listed without a
    # second code change; it is a no-op while protection stays disabled.
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=8000,
        stateless_http=True,
        allowed_hosts=ALLOWED_HOSTS,
    )


if __name__ == "__main__":
    main()
