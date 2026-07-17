"""FastMCP 3.x server exposing the self-docs retrieval tools over streamable
HTTP (stateless — a search tool needs no session state, and stateless mode
survives restarts/load-balancing behind Traefik).
"""

from __future__ import annotations

import structlog
from fastmcp import FastMCP
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

mcp = FastMCP("self-docs")


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
    mcp.run(transport="http", host="0.0.0.0", port=8000, stateless_http=True)


if __name__ == "__main__":
    main()
