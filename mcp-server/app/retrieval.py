"""Hybrid (vector + full-text) retrieval over the doc_chunks/doc_pages/doc_sources
schema owned by T1, plus formatting helpers used by the MCP tools in server.py.

Retrieval strategy: Reciprocal Rank Fusion (RRF, k=60) over two arms, each capped
at the top 30 candidates:
  - vector arm:  cosine distance (`<=>`) over the HNSW index on doc_chunks.embedding
  - fts arm:     ts_rank over the generated `fts` tsvector column, filtered by
                 `websearch_to_tsquery('english', query)`

Both arms are computed in a single SQL statement (two nested CTEs each, so the
window-based rank is computed only over the already-LIMIT-30 candidate set,
letting the planner use the HNSW/GIN indexes for the expensive part instead of
ranking the whole table).
"""

from __future__ import annotations

import atexit
import hashlib
import ipaddress
import os
import socket
import time
from typing import Any
from urllib.parse import urlparse

import psycopg
import structlog
from fastembed import TextEmbedding
from prometheus_client import Counter, Histogram
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, field_validator, model_validator

logger = structlog.get_logger(__name__)

EMBEDDING_MODEL_NAME = "BAAI/bge-small-en-v1.5"
RRF_K = 60
ARM_CANDIDATE_LIMIT = 30

SEARCH_REQUESTS_TOTAL = Counter(
    "search_requests_total",
    "Total number of search_docs calls",
    ["status"],
)
SEARCH_LATENCY_SECONDS = Histogram(
    "search_latency_seconds",
    "Latency of search_docs calls in seconds",
)

# Hybrid RRF search: fuse a top-30 vector-similarity arm with a top-30 full-text
# arm using Reciprocal Rank Fusion (score = sum over arms of 1/(k + rank)).
# `%(source)s` is NULL when no source filter is requested; the `IS NULL OR`
# guard makes the filter optional without a second query string.
HYBRID_SEARCH_SQL = """
WITH vector_candidates AS (
    SELECT dc.id, dc.embedding <=> %(query_vec)s::vector AS distance
    FROM doc_chunks dc
    JOIN doc_pages dp ON dp.id = dc.page_id
    JOIN doc_sources ds ON ds.id = dp.source_id
    WHERE %(source)s::text IS NULL OR ds.name = %(source)s
    ORDER BY dc.embedding <=> %(query_vec)s::vector
    LIMIT {arm_limit}
),
vector_arm AS (
    SELECT id, row_number() OVER (ORDER BY distance) AS rnk
    FROM vector_candidates
),
fts_candidates AS (
    SELECT dc.id,
           ts_rank(dc.fts, websearch_to_tsquery('english', %(query_text)s)) AS rank_score
    FROM doc_chunks dc
    JOIN doc_pages dp ON dp.id = dc.page_id
    JOIN doc_sources ds ON ds.id = dp.source_id
    WHERE dc.fts @@ websearch_to_tsquery('english', %(query_text)s)
      AND (%(source)s::text IS NULL OR ds.name = %(source)s)
    ORDER BY rank_score DESC
    LIMIT {arm_limit}
),
fts_arm AS (
    SELECT id, row_number() OVER (ORDER BY rank_score DESC) AS rnk
    FROM fts_candidates
),
fused AS (
    SELECT id, SUM(1.0 / ({rrf_k} + rnk)) AS rrf_score
    FROM (
        SELECT id, rnk FROM vector_arm
        UNION ALL
        SELECT id, rnk FROM fts_arm
    ) arms
    GROUP BY id
)
SELECT dc.heading_path, dp.url, dc.content, fused.rrf_score
FROM fused
JOIN doc_chunks dc ON dc.id = fused.id
JOIN doc_pages dp ON dp.id = dc.page_id
ORDER BY fused.rrf_score DESC
LIMIT %(limit)s;
""".format(arm_limit=ARM_CANDIDATE_LIMIT, rrf_k=RRF_K)

LIST_SOURCES_SQL = """
SELECT
    ds.name,
    ds.last_synced,
    ds.last_status,
    COUNT(dc.id) AS chunk_count
FROM doc_sources ds
LEFT JOIN doc_pages dp ON dp.source_id = ds.id
LEFT JOIN doc_chunks dc ON dc.page_id = dp.id
GROUP BY ds.id, ds.name, ds.last_synced, ds.last_status
ORDER BY ds.name;
"""

_pool: ConnectionPool | None = None
_embedding_model: TextEmbedding | None = None


def _build_conninfo() -> str:
    """Build a libpq conninfo string from POSTGRES_* env vars (service name `db`
    is the default host, matching the compose network DNS)."""
    host = os.environ.get("POSTGRES_HOST", "db")
    port = os.environ.get("POSTGRES_PORT", "5432")
    user = os.environ.get("POSTGRES_USER", "postgres")
    password = os.environ.get("POSTGRES_PASSWORD", "postgres")
    dbname = os.environ.get("POSTGRES_DB", "postgres")
    return f"host={host} port={port} user={user} password={password} dbname={dbname}"


def get_pool() -> ConnectionPool:
    """Return the process-wide connection pool, opening it lazily on first use
    so that importing/starting the server never requires a live database."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=_build_conninfo(),
            min_size=1,
            max_size=5,
            open=False,
            kwargs={"row_factory": dict_row},
        )
        _pool.open(wait=False)
        atexit.register(_pool.close)
    return _pool


def get_embedding_model() -> TextEmbedding:
    """Return the process-wide FastEmbed model, loading it lazily on first use."""
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = TextEmbedding(model_name=EMBEDDING_MODEL_NAME)
    return _embedding_model


def _format_vector_literal(vec: Any) -> str:
    """Render an embedding as a pgvector text literal, e.g. "[0.1,0.2,...]"."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _embed_query(query: str) -> str:
    """Embed a search query with FastEmbed's `query_embed` (applies the BGE
    `query:` prefix — asymmetric to `passage_embed` used at ingest time)."""
    model = get_embedding_model()
    (vector,) = list(model.query_embed([query]))
    return _format_vector_literal(vector)


def format_hit(heading_path: str | None, url: str, content: str) -> str:
    """Format a single search hit exactly per the MCP tool contract:
    "### {heading_path}\\n[{url}]({url})\\n\\n{content}"."""
    heading = heading_path or ""
    return f"### {heading}\n[{url}]({url})\n\n{content}"


def search(query: str, source: str | None = None, limit: int = 5) -> str:
    """Run the hybrid RRF search and return the formatted markdown result string
    (hits joined by "\\n\\n---\\n\\n"), recording request/latency metrics."""
    start = time.perf_counter()
    status = "error"
    try:
        query_vec = _embed_query(query)
        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    HYBRID_SEARCH_SQL,
                    {
                        "query_vec": query_vec,
                        "query_text": query,
                        "source": source,
                        "limit": limit,
                    },
                )
                rows = cur.fetchall()
        status = "ok"
    except Exception:
        logger.exception("search_docs_failed", query=query, source=source)
        raise
    finally:
        SEARCH_LATENCY_SECONDS.observe(time.perf_counter() - start)
        SEARCH_REQUESTS_TOTAL.labels(status=status).inc()

    if not rows:
        return "No matching documentation found."

    hits = [
        format_hit(row["heading_path"], row["url"], row["content"]) for row in rows
    ]
    logger.info("search_docs", query=query, source=source, hit_count=len(hits))
    return "\n\n---\n\n".join(hits)


def list_sources() -> str:
    """List indexed documentation sources with last-sync time, last status, and
    chunk count, formatted as markdown."""
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(LIST_SOURCES_SQL)
            rows = cur.fetchall()

    if not rows:
        return "No documentation sources indexed yet."

    lines = ["| source | last_synced | last_status | chunks |", "|---|---|---|---|"]
    for row in rows:
        last_synced = row["last_synced"].isoformat() if row["last_synced"] else "never"
        lines.append(
            f"| {row['name']} | {last_synced} | {row['last_status'] or 'unknown'} "
            f"| {row['chunk_count']} |"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# propose_doc_source: agent-facing INSERT of a status='pending' doc_sources
# row for later human approval in the admin UI.
#
# mcp-server is a separate package/build context from ingestion (its own
# venv, its own Docker build context — see docker-compose.yml's `mcp-server`
# service) and cannot `import ingestion.app.config`. `ProposedSourceConfig`
# below is therefore a DELIBERATE, FIELD-FOR-FIELD MIRROR of
# `ingestion/app/config.py`'s `SourceConfig` (same pattern, same HttpUrl
# typing, same prefix-filter invariant, same `extra="forbid"`) so that a
# proposal accepted here can never be rejected by ingestion's own validator
# once a human approves it. KEEP THESE TWO CLASSES IN SYNC if either changes.
# ---------------------------------------------------------------------------

NAME_PATTERN = r"^[a-z0-9-]+$"


class ProposalError(ValueError):
    """Raised when a propose_doc_source call is rejected: invalid
    configuration (fails ProposedSourceConfig validation) or a duplicate
    `name` (UNIQUE violation on doc_sources.name). Message is human-readable
    and safe to return directly to the calling agent."""


def _path_allowed(path: str, include_prefixes: list[str], exclude_prefixes: list[str]) -> bool:
    """Mirrors ingestion/app/config.py's `_path_allowed` (itself mirroring
    crawler._allowed): exclude_prefixes always wins; empty include_prefixes
    allowlists everything."""
    if any(path.startswith(p) for p in exclude_prefixes):
        return False
    if include_prefixes:
        return any(path.startswith(p) for p in include_prefixes)
    return True


# ---------------------------------------------------------------------------
# SSRF guard (security review H1/H2) — DELIBERATE PARALLEL COPY of
# ingestion/app/urlscope.py's `_addr_is_private` / `_resolve_is_private` /
# `url_host_is_private`. mcp-server cannot import ingestion (see module note
# above `ProposedSourceConfig`), so these helpers are duplicated here on
# purpose. KEEP THE TWO IMPLEMENTATIONS BYTE-FOR-BEHAVIOR-EQUIVALENT: if you
# change the classification rules or the fail-closed semantics in
# ingestion/app/urlscope.py, change them here too, in the same commit. This is
# the exact drift scenario the security review flagged — a private-address
# check added only to ingestion's `SourceConfig` would leave THIS admin-form
# entry point (propose_doc_source) as an unguarded SSRF vector, which is the
# worse of the two failure modes (the other being merely "proposal accepted
# here, rejected later at approval").
# ---------------------------------------------------------------------------


def _addr_is_private(addr: str) -> bool:
    """True if a *literal* address string is in a non-routable range."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.is_private or ip.is_link_local or ip.is_loopback or ip.is_reserved


def _resolve_is_private(host: str) -> bool:
    """True if `host` is, or resolves to, a private/loopback/link-local/
    reserved address.

    Resolution is delegated to the OS resolver, which normalizes the
    decimal/octal/hex integer forms of an IPv4 literal (`2130706433`,
    `0177.0.0.1`) that `ipaddress.ip_address` alone rejects — running
    `ip_address()` over the `getaddrinfo` RESULT therefore closes those
    encodings for free.

    FAILS CLOSED: an unresolvable host returns True. An unresolvable host is
    not fetchable anyway, so treating it as private costs nothing and avoids
    a resolver hiccup silently opening the gate.
    """
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, ValueError):
        return True
    if not infos:
        return True
    return any(_addr_is_private(info[4][0]) for info in infos)


def _url_host_is_private(url: str) -> bool:
    """True if `url`'s host is, or resolves to, a non-routable address. A URL
    with no parseable host fails closed (True)."""
    try:
        host = urlparse(url).hostname
    except ValueError:
        return True
    return _resolve_is_private(host or "")


class ProposedSourceConfig(BaseModel):
    """A single proposed doc-source entry. Field-for-field mirror of
    ingestion/app/config.py's `SourceConfig` — see module note above."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    base_url: HttpUrl
    sitemap: HttpUrl | None = None
    include_prefixes: list[str] = Field(default_factory=list)
    exclude_prefixes: list[str] = Field(default_factory=list)
    max_pages: int = Field(gt=0)
    language: str = "english"
    rate_limit_rps: float = Field(default=1.0, gt=0)

    @field_validator("include_prefixes", "exclude_prefixes", mode="before")
    @classmethod
    def _none_to_empty(cls, v: Any) -> Any:
        return v if v is not None else []

    @model_validator(mode="after")
    def _sitemap_shares_base_url_host(self) -> "ProposedSourceConfig":
        # SSRF guard (security review H1): `sitemap` is fetched BEFORE any of
        # its `<loc>` entries are host-filtered, and a `<sitemapindex>` fans
        # out to its children equally unvalidated. Constraining the sitemap to
        # base_url's host closes both: the root request is in-scope by
        # construction and every child is checkable against the same host.
        # Mirrors ingestion/app/config.py's `SourceConfig._sitemap_shares_base_url_host`.
        if self.sitemap is None:
            return self
        base_host = urlparse(str(self.base_url)).netloc
        sitemap_host = urlparse(str(self.sitemap)).netloc
        if sitemap_host != base_host:
            raise ValueError(
                f"source '{self.name}': sitemap host {sitemap_host!r} differs from base_url "
                f"host {base_host!r} — a sitemap is fetched before its entries are "
                "host-filtered, so an off-host sitemap is a server-side request forgery "
                "vector. Point the sitemap at base_url's own host."
            )
        return self

    @model_validator(mode="after")
    def _hosts_must_not_be_private(self) -> "ProposedSourceConfig":
        # SSRF guard (security review H2): source URLs are untrusted input (an
        # MCP tool callable by an AI agent), so reject a host that IS or
        # RESOLVES TO private/loopback/link-local/reserved space at proposal
        # time — before a human is ever shown an approval prompt. Fails closed
        # on an unresolvable host. Mirrors ingestion/app/config.py's
        # `SourceConfig._hosts_must_not_be_private`; see this module's
        # `_resolve_is_private` for the accepted DNS-rebinding residual.
        for field_name, value in (("base_url", self.base_url), ("sitemap", self.sitemap)):
            if value is None:
                continue
            if _url_host_is_private(str(value)):
                raise ValueError(
                    f"source '{self.name}': {field_name} host "
                    f"{urlparse(str(value)).hostname!r} is, resolves to, or cannot be "
                    "resolved away from a private/loopback/link-local/reserved address — "
                    "refusing to propose a source that could crawl internal network space."
                )
        return self

    @model_validator(mode="after")
    def _base_url_passes_own_prefix_filters(self) -> "ProposedSourceConfig":
        # Same rationale as SourceConfig's validator of the same name: a
        # sitemap-less source whose base_url path is excluded by its own
        # prefixes would crawl 0 pages if ever approved. Reject at proposal
        # time so a human reviewer never has to discover this manually.
        if self.sitemap is not None:
            return self
        path = urlparse(str(self.base_url)).path or "/"
        if not _path_allowed(path, self.include_prefixes, self.exclude_prefixes):
            raise ValueError(
                f"source '{self.name}': base_url path {path!r} is excluded by its own "
                f"include_prefixes={self.include_prefixes!r} / exclude_prefixes="
                f"{self.exclude_prefixes!r} — the BFS crawl seed would be filtered out "
                "before the first fetch, so this source would index 0 pages if approved. "
                "Fix the prefixes so base_url's path itself is allowed."
            )
        return self


def derive_proposed_by(token: str) -> str:
    """Derive a non-secret identifier for `doc_sources.proposed_by` from an
    authenticated MCP bearer token: a truncated SHA-256 hex digest, never the
    token itself. `proposed_by` is rendered into the admin UI, so storing the
    live token would put a live credential on an HTML page."""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"token-{digest[:16]}"


# `status` is a hardcoded SQL literal ('pending'), never a bind parameter —
# this is what makes it structurally impossible for any argument combination
# passed through propose_source() to create an 'active' or 'rejected' row.
PROPOSE_SOURCE_SQL = """
INSERT INTO doc_sources
    (name, base_url, sitemap, include_prefixes, exclude_prefixes,
     max_pages, language, rate_limit_rps, status, proposed_by)
VALUES
    (%(name)s, %(base_url)s, %(sitemap)s, %(include_prefixes)s, %(exclude_prefixes)s,
     %(max_pages)s, %(language)s, %(rate_limit_rps)s, 'pending', %(proposed_by)s)
RETURNING id;
"""


def propose_source(
    *,
    name: str,
    base_url: str,
    max_pages: int,
    sitemap: str | None = None,
    include_prefixes: list[str] | None = None,
    exclude_prefixes: list[str] | None = None,
    language: str = "english",
    rate_limit_rps: float = 1.0,
    proposed_by_token: str,
) -> int:
    """Validate a proposed source via `ProposedSourceConfig` and insert a
    `status='pending'` `doc_sources` row, returning its id.

    Raises `ProposalError` (never a raw exception) on:
      - invalid configuration (bad name/URL/max_pages/rate_limit_rps, the
        BFS-seed prefix-filter check, or an unknown field), or
      - a duplicate `name` (UNIQUE violation), regardless of the existing
        row's status — a name pending, active, or rejected is still taken.

    There is no `status` parameter: the INSERT's `status` column is a fixed
    SQL literal (see `PROPOSE_SOURCE_SQL`), so no argument combination can
    make this function write anything but 'pending'.
    """
    try:
        cfg = ProposedSourceConfig(
            name=name,
            base_url=base_url,
            sitemap=sitemap,
            include_prefixes=include_prefixes or [],
            exclude_prefixes=exclude_prefixes or [],
            max_pages=max_pages,
            language=language,
            rate_limit_rps=rate_limit_rps,
        )
    except ValidationError as e:
        raise ProposalError(f"invalid source configuration: {e}") from e

    params = {
        "name": cfg.name,
        "base_url": str(cfg.base_url),
        "sitemap": str(cfg.sitemap) if cfg.sitemap is not None else None,
        "include_prefixes": list(cfg.include_prefixes),
        "exclude_prefixes": list(cfg.exclude_prefixes),
        "max_pages": cfg.max_pages,
        "language": cfg.language,
        "rate_limit_rps": cfg.rate_limit_rps,
        "proposed_by": derive_proposed_by(proposed_by_token),
    }

    pool = get_pool()
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(PROPOSE_SOURCE_SQL, params)
                row = cur.fetchone()
    except psycopg.errors.UniqueViolation as e:
        raise ProposalError(
            f"a source named '{cfg.name}' already exists (pending, active, or "
            "rejected) — choose a different name, or ask a human to review the "
            "existing entry in the admin UI"
        ) from e

    assert row is not None
    logger.info("propose_doc_source", name=cfg.name, source_id=row["id"])
    return row["id"]
