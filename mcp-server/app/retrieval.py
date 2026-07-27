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
import re
import socket
import time
from collections.abc import Collection
from typing import Any, Literal
from urllib.parse import urlparse

import psycopg
import structlog
from fastembed import TextEmbedding
from prometheus_client import Counter, Histogram
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, field_validator, model_validator

from app.source_defaults import _slugify, apply_creation_defaults

logger = structlog.get_logger(__name__)

# Model, and the per-model QUERY prompt, are env-driven (set by `make configure`
# from config/models.yaml), defaulting to the registry default so a fresh
# checkout / CI works unconfigured. FastEmbed applies no prefix for the models
# we support, so the query instruction is prepended manually in `_embed_query`
# — the asymmetric counterpart to ingestion's EMBEDDING_PASSAGE_PROMPT. The two
# services MUST run the same model for the vectors to be comparable.
# Fallbacks used when EMBEDDING_* env is unset. These MUST equal the registry
# default row in config/models.yaml — the parity tests enforce it.
DEFAULT_MODEL_NAME = "mixedbread-ai/mxbai-embed-large-v1"
DEFAULT_QUERY_PROMPT = "Represent this sentence for searching relevant passages: "
# PASSAGE_PROMPT is the mcp-server-side counterpart of ingestion/app/embedder.py's
# EMBEDDING_PASSAGE_PROMPT, used only by upload_text (T11) to embed
# MCP-uploaded content on the same asymmetric passage side ingestion's crawl
# pipeline uses — empty for mxbai/bge, same env var name so a `make configure`
# run that sets EMBEDDING_PASSAGE_PROMPT for ingestion also takes effect here.
DEFAULT_PASSAGE_PROMPT = ""
EMBEDDING_MODEL_NAME = os.environ.get("EMBEDDING_MODEL_NAME", DEFAULT_MODEL_NAME)
QUERY_PROMPT = os.environ.get("EMBEDDING_QUERY_PROMPT", DEFAULT_QUERY_PROMPT)
PASSAGE_PROMPT = os.environ.get("EMBEDDING_PASSAGE_PROMPT", DEFAULT_PASSAGE_PROMPT)
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
HYBRID_SEARCH_SQL = f"""
WITH vector_candidates AS (
    SELECT dc.id, dc.embedding <=> %(query_vec)s::vector AS distance
    FROM doc_chunks dc
    JOIN doc_pages dp ON dp.id = dc.page_id
    JOIN doc_sources ds ON ds.id = dp.source_id
    WHERE %(source)s::text IS NULL OR ds.name = %(source)s
    ORDER BY dc.embedding <=> %(query_vec)s::vector
    LIMIT {ARM_CANDIDATE_LIMIT}
),
vector_arm AS (
    SELECT id, row_number() OVER (ORDER BY distance) AS rnk
    FROM vector_candidates
),
fts_candidates AS (
    SELECT dc.id,
           ts_rank(dc.fts, websearch_to_tsquery(dc.fts_config, %(query_text)s)) AS rank_score
    FROM doc_chunks dc
    JOIN doc_pages dp ON dp.id = dc.page_id
    JOIN doc_sources ds ON ds.id = dp.source_id
    WHERE dc.fts @@ websearch_to_tsquery(dc.fts_config, %(query_text)s)
      AND (%(source)s::text IS NULL OR ds.name = %(source)s)
    ORDER BY rank_score DESC
    LIMIT {ARM_CANDIDATE_LIMIT}
),
fts_arm AS (
    SELECT id, row_number() OVER (ORDER BY rank_score DESC) AS rnk
    FROM fts_candidates
),
fused AS (
    SELECT id, SUM(1.0 / ({RRF_K} + rnk)) AS rrf_score
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
"""

LIST_SOURCES_SQL = """
SELECT
    ds.name,
    ds.last_synced,
    ds.last_status,
    COUNT(dc.id) AS chunk_count,
    ds.source_type
FROM doc_sources ds
LEFT JOIN doc_pages dp ON dp.source_id = ds.id
LEFT JOIN doc_chunks dc ON dc.page_id = dp.id
GROUP BY ds.id, ds.name, ds.last_synced, ds.last_status, ds.source_type
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
    """Embed a search query, prepending EMBEDDING_QUERY_PROMPT (the per-model
    query instruction — asymmetric to ingestion's EMBEDDING_PASSAGE_PROMPT).
    FastEmbed applies no prefix for the supported models, so we prepend it and
    use plain `embed()`."""
    model = get_embedding_model()
    (vector,) = list(model.embed([QUERY_PROMPT + query]))
    return _format_vector_literal(vector)


def _embed_passage(text: str) -> str:
    """Embed a document/passage chunk (upload_text's use case), prepending
    PASSAGE_PROMPT — the asymmetric counterpart to `_embed_query`, mirroring
    ingestion/app/embedder.py's `embed_chunks` prompt handling for a single
    chunk at a time (upload_text's expected batch sizes are small enough that
    ingestion's batched-embed call shape isn't needed here)."""
    model = get_embedding_model()
    (vector,) = list(model.embed([PASSAGE_PROMPT + text]))
    return _format_vector_literal(vector)


def format_hit(heading_path: str | None, url: str, content: str) -> str:
    """Format a single search hit exactly per the MCP tool contract:
    "### {heading_path}\\n[{url}]({url})\\n\\n{content}".

    Exception: a non-http(s) `url` (i.e. one that doesn't start with
    `http://` or `https://` — this covers the `upload://{source}/{path}`
    sentinel scheme written by `upload_text`) renders as plain text instead
    of a markdown link. A markdown link whose target isn't a fetchable
    http(s) URL is a dead/unusable link in an AI agent's output, so the
    sentinel is shown as bare text instead."""
    heading = heading_path or ""
    if url.startswith("http://") or url.startswith("https://"):
        url_line = f"[{url}]({url})"
    else:
        url_line = url
    return f"### {heading}\n{url_line}\n\n{content}"


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

    lines = [
        "| source | last_synced | last_status | chunks | source_type |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        last_synced = row["last_synced"].isoformat() if row["last_synced"] else "never"
        lines.append(
            f"| {row['name']} | {last_synced} | {row['last_status'] or 'unknown'} "
            f"| {row['chunk_count']} | {row['source_type']} |"
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

# Placeholder / documentation-example hosts (RFC 2606, RFC 6761) plus a
# handful of test fixtures that have leaked into the live index before (T13
# live-database audit: 's1'/'s2' sources, a 'docs.company.com' page indexed
# under a trusted source name). Rejected outright at proposal time so they
# can never be re-added. DELIBERATE MIRROR of ingestion/app/config.py's
# `PLACEHOLDER_HOSTS` / `PLACEHOLDER_HOST_SUFFIXES` — see module note above
# `ProposedSourceConfig`.
PLACEHOLDER_HOSTS = frozenset(
    {
        "example.com",
        "example.org",
        "example.net",
        "docs.company.com",
        "localhost",
    }
)
PLACEHOLDER_HOST_SUFFIXES = (".test", ".invalid", ".example", ".localhost")

# Postgres built-in text-search configuration names (see `\dF` / pg_catalog
# `pg_ts_config` in a default install). `language` must be one of these,
# lowercased, since it is passed straight through to `to_tsvector(language, ...)`
# / `to_tsquery(language, ...)` — an invalid name errors at query time, not at
# proposal time, so we validate it up front instead. DELIBERATE MIRROR of
# ingestion/app/config.py's `SUPPORTED_FTS_LANGUAGES` — see module note above
# `ProposedSourceConfig`.
SUPPORTED_FTS_LANGUAGES = frozenset(
    {
        "simple",
        "arabic",
        "armenian",
        "basque",
        "catalan",
        "danish",
        "dutch",
        "english",
        "finnish",
        "french",
        "german",
        "greek",
        "hindi",
        "hungarian",
        "indonesian",
        "irish",
        "italian",
        "lithuanian",
        "nepali",
        "norwegian",
        "portuguese",
        "romanian",
        "russian",
        "serbian",
        "spanish",
        "swedish",
        "tamil",
        "turkish",
        "yiddish",
    }
)


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
    if os.environ.get("SELF_DOCS_ALLOW_PRIVATE_ADDRESSES") == "1":
        return False
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
    if os.environ.get("SELF_DOCS_ALLOW_PRIVATE_ADDRESSES") == "1":
        return False
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
    if os.environ.get("SELF_DOCS_ALLOW_PRIVATE_ADDRESSES") == "1":
        return False
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
    # Field-for-field mirror of ingestion/app/config.py's SourceConfig.source_type
    # (T2) — kept in sync by the drift-guard test
    # test_proposed_source_config_stays_in_sync_with_ingestion_source_config.
    # NOTE: propose_doc_source (the only caller that builds a ProposedSourceConfig)
    # never sets this to anything but the 'crawl' default — this tool proposes
    # CRAWL sources only, per CLAUDE.md's propose->human-approve model. An
    # upload-type source can only be created by a human via the admin UI
    # (T8/T9); adding a value here does NOT grant any new capability.
    source_type: Literal["crawl", "upload"] = "crawl"
    # NOT `HttpUrl` (field-for-field mirror requirement — the drift-guard test
    # compares annotations directly): ingestion/app/config.py's `SourceConfig`
    # switched this to a plain `str` in T2 because an upload-type source's
    # base_url is the non-http(s) `upload://{name}` sentinel. http(s)-URL
    # validation for `source_type='crawl'` (the only value propose_doc_source
    # ever produces) now happens in `_base_url_matches_source_type` below
    # instead of via the field's own type, exactly mirroring SourceConfig.
    base_url: str
    sitemap: HttpUrl | None = None
    include_prefixes: list[str] = Field(default_factory=list)
    exclude_prefixes: list[str] = Field(default_factory=list)
    max_pages: int | None = Field(default=None, gt=0)
    language: str = "english"
    rate_limit_rps: float = Field(default=1.0, gt=0)
    llms_txt: Literal["auto", "off", "only"] = "auto"
    js_render: bool = False

    @field_validator("include_prefixes", "exclude_prefixes", mode="before")
    @classmethod
    def _none_to_empty(cls, v: Any) -> Any:
        return v if v is not None else []

    @field_validator("language", mode="after")
    @classmethod
    def _language_must_be_supported(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in SUPPORTED_FTS_LANGUAGES:
            raise ValueError(
                f"language {v!r} is not a supported Postgres text-search configuration "
                f"— must be one of SUPPORTED_FTS_LANGUAGES: {sorted(SUPPORTED_FTS_LANGUAGES)}"
            )
        return normalized

    @model_validator(mode="after")
    def _base_url_matches_source_type(self) -> ProposedSourceConfig:
        # Mirrors ingestion/app/config.py's `SourceConfig._base_url_matches_source_type`
        # byte-for-behavior-equivalent (see module note above `ProposedSourceConfig`).
        # propose_doc_source never sets source_type='upload' (only the admin UI
        # can create an upload-type source), so the `upload` branch below is
        # unreachable in practice today — it exists purely so this validator,
        # and therefore the field it guards, stays a true field-for-field
        # mirror of SourceConfig for the drift-guard test.
        if self.source_type == "upload":
            expected = f"upload://{self.name}"
            if self.base_url != expected:
                raise ValueError(
                    f"source '{self.name}': an upload-type source's base_url must be "
                    f"exactly {expected!r} (the upload sentinel URL) — got "
                    f"{self.base_url!r}"
                )
            return self

        try:
            parsed = HttpUrl(self.base_url)
        except ValidationError as e:
            raise ValueError(
                f"source '{self.name}': base_url {self.base_url!r} is not a valid "
                f"http(s) URL: {e}"
            ) from e
        self.base_url = str(parsed)
        return self

    @model_validator(mode="after")
    def _sitemap_shares_base_url_host(self) -> ProposedSourceConfig:
        # SSRF guard (security review H1): `sitemap` is fetched BEFORE any of
        # its `<loc>` entries are host-filtered, and a `<sitemapindex>` fans
        # out to its children equally unvalidated. Constraining the sitemap to
        # base_url's host closes both: the root request is in-scope by
        # construction and every child is checkable against the same host.
        # Mirrors ingestion/app/config.py's `SourceConfig._sitemap_shares_base_url_host`.
        if self.source_type == "upload" or self.sitemap is None:
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
    def _hosts_must_not_be_private(self) -> ProposedSourceConfig:
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
    def _base_url_must_not_be_placeholder(self) -> ProposedSourceConfig:
        # T13 live-database audit: test/placeholder rows (RFC 2606/6761
        # reserved names like example.com, plus a fixture 'docs.company.com'
        # page that got indexed under a trusted source name) leaked into the
        # production doc index. Reject them at proposal time. Mirrors
        # ingestion/app/config.py's `SourceConfig._base_url_must_not_be_placeholder`.
        for field_name, value in (("base_url", self.base_url), ("sitemap", self.sitemap)):
            if value is None:
                continue
            host = (urlparse(str(value)).hostname or "").lower()
            if host in PLACEHOLDER_HOSTS or host.endswith(PLACEHOLDER_HOST_SUFFIXES):
                raise ValueError(
                    f"source '{self.name}': {field_name} host {host!r} is a placeholder/"
                    "reserved documentation-example host (RFC 2606/6761) — not a real "
                    "documentation site. Use the actual source URL."
                )
        return self

    @model_validator(mode="after")
    def _base_url_passes_own_prefix_filters(self) -> ProposedSourceConfig:
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
    name: str | None = None,
    base_url: str,
    max_pages: int | None = None,
    sitemap: str | None = None,
    include_prefixes: list[str] | None = None,
    exclude_prefixes: list[str] | None = None,
    language: str = "english",
    rate_limit_rps: float = 1.0,
    proposed_by_token: str,
) -> int:
    """Validate a proposed source via `ProposedSourceConfig` and insert a
    `status='pending'` `doc_sources` row, returning its id.

    `name` is OPTIONAL. Whichever of `name`/`include_prefixes`/`max_pages`
    is OMITTED (not supplied as an argument, i.e. left at its `None`
    default) is derived from `base_url`, independent of whether the other
    two were supplied: a unique `^[a-z0-9-]+$` slug when `name` is omitted
    (see `source_defaults.derive_name`, made unique against every existing
    `doc_sources.name` regardless of status, via one extra `SELECT` only
    run in that case), and/or scoped `include_prefixes`/a `DEFAULT_MAX_PAGES`
    ceiling when those are omitted (`source_defaults.apply_creation_defaults`).
    This derivation is what makes an under-specified proposal safe: an empty
    `include_prefixes` allows the ENTIRE host and a `None` `max_pages` means
    no page ceiling at all (see `source_defaults` module docstring) —
    leaving only same-host as a bound would let a proposal on a shared docs
    host crawl an entire unrelated product's documentation, whether or not
    a `name` was supplied.

    `max_pages` has no supported "unlimited" opt-out: because its default is
    `None`, an explicit `max_pages=None` is indistinguishable from omitting
    the argument, so BOTH always receive the derived `DEFAULT_MAX_PAGES`
    ceiling. To propose an uncapped crawl, pass an explicit positive
    `max_pages` large enough for your use case — there is no way to request
    a literal `None`/no-ceiling `max_pages` through this function.

    Raises `ProposalError` (never a raw exception) on:
      - invalid configuration (bad name/URL/max_pages/rate_limit_rps, the
        BFS-seed prefix-filter check, or an unknown field), or
      - a duplicate `name` (UNIQUE violation), regardless of the existing
        row's status — a name pending, active, or rejected is still taken.

    There is no `status` parameter: the INSERT's `status` column is a fixed
    SQL literal (see `PROPOSE_SOURCE_SQL`), so no argument combination can
    make this function write anything but 'pending'.
    """
    pool = get_pool()
    effective_name = name

    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                if name is None:
                    # Only queried in URL-only mode: every other test/call
                    # path supplies an explicit name and must keep making
                    # exactly one INSERT round-trip.
                    cur.execute("SELECT name FROM doc_sources;", None)
                    taken: Collection[str] = {row["name"] for row in cur.fetchall()}
                else:
                    taken = ()

                # Build `fields` from whichever of name/include_prefixes/
                # max_pages are actually absent, independent of one another —
                # a caller who supplies `name` but omits `include_prefixes`/
                # `max_pages` must still get derived scoping/cap (see
                # `apply_creation_defaults`'s absent-vs-empty-vs-None
                # semantics). `derive_name` is only invoked when `name` is
                # missing, since `taken` above is only populated in that case.
                fields: dict[str, Any] = {"base_url": base_url}
                if name is not None:
                    fields["name"] = name
                if include_prefixes is not None:
                    fields["include_prefixes"] = include_prefixes
                if max_pages is not None:
                    fields["max_pages"] = max_pages
                fields = apply_creation_defaults(fields, taken)
                effective_name = fields["name"]
                effective_include_prefixes = fields["include_prefixes"]
                effective_max_pages = fields["max_pages"]

                try:
                    cfg = ProposedSourceConfig(
                        name=effective_name,
                        base_url=base_url,
                        sitemap=sitemap,
                        include_prefixes=effective_include_prefixes or [],
                        exclude_prefixes=exclude_prefixes or [],
                        max_pages=effective_max_pages,
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
                cur.execute(PROPOSE_SOURCE_SQL, params)
                row = cur.fetchone()
    except psycopg.errors.UniqueViolation as e:
        raise ProposalError(
            f"a source named '{effective_name}' already exists (pending, active, or "
            "rejected) — choose a different name, or ask a human to review the "
            "existing entry in the admin UI"
        ) from e

    assert row is not None
    logger.info("propose_doc_source", name=cfg.name, source_id=row["id"])
    return row["id"]


# ---------------------------------------------------------------------------
# upload_doc_text (T11): agent-facing write of Markdown/text content into an
# EXISTING `source_type='upload'` doc_sources row. Unlike propose_source
# above (which only ever INSERTs a status='pending' doc_sources row for a
# human to approve later), this function can NEVER create or modify a
# doc_sources row — it only ever reads one (by name) to check its id/
# source_type/language, then writes doc_pages/doc_chunks for that existing
# source id. That is what makes it structurally impossible for this tool to
# create a source or write into a crawl-type source: there is no `INSERT
# INTO doc_sources` anywhere in this function, and the `source_type != 'upload'`
# check below runs before any doc_pages/doc_chunks write.
#
# mcp-server cannot import ingestion/app/store.py (separate package/build
# context — see the module note above `ProposedSourceConfig`), so the
# chunk/embed/insert primitives below are a deliberately MINIMAL, PURPOSE-BUILT
# duplicate rather than a full mirror of ingestion's pipeline:
#   - chunking: `_chunk_upload_content` is a simplified sibling of
#     ingestion/app/chunker.py's heading-aware, tokenizer-windowed chunker —
#     same heading-breadcrumb idea, but windowed by a character budget
#     instead of an actual BGE token count (no code-fence-aware unit
#     splitting, no overlap), so this file does not need a hard dependency on
#     `tokenizers`/downloading a tokenizer.json for a feature whose expected
#     input (an agent-pasted note, capped at 1 MB) is short-form text rather
#     than a full crawled page.
#   - embedding: `_embed_passage` reuses the SAME `get_embedding_model()` /
#     `_format_vector_literal` primitives `_embed_query` already uses for
#     search, just on the passage side (PASSAGE_PROMPT instead of
#     QUERY_PROMPT) — this is the asymmetric counterpart already established
#     in this file, adapted rather than reinvented.
#   - insert: `upload_text` replicates `ingestion/app/store.py`'s
#     `replace_page`'s exact DELETE-then-INSERT shape and its
#     `hash_markdown`-style SHA-256 `content_hash` convention (delete any
#     existing doc_pages row for this url, insert a fresh one, insert its
#     chunks) rather than reinventing a different write shape.
# ---------------------------------------------------------------------------

MAX_UPLOAD_CONTENT_BYTES = 1_000_000  # 1 MB
MAX_UPLOAD_TITLE_CHARS = 200

# Char budget for _chunk_upload_content: a coarse proxy for
# ingestion/app/chunker.py's MIN_TOKENS..MAX_TOKENS window (400-600 BGE
# tokens) using ~4 chars/BGE-subword-token as a standard rule-of-thumb for
# English prose (~4 * 500 =~ 2000), instead of an actual tokenizer count —
# see the module note above this section for why.
UPLOAD_CHUNK_CHAR_BUDGET = 2000

_UPLOAD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


class UploadError(ValueError):
    """Raised when an upload_doc_text call is rejected: unknown source name,
    a source that isn't source_type='upload', or content/title exceeding
    their size caps. Message is human-readable and safe to return directly
    to the calling agent."""


def _split_upload_into_heading_sections(content: str) -> list[tuple[str, str]]:
    """Split `content` into (heading_path, section_text) pairs on markdown
    heading boundaries (# .. ######), tracking a breadcrumb the same way
    ingestion/app/chunker.py's `_split_into_heading_segments` does. A
    deliberately simpler sibling (no fenced-code-block handling) — see the
    module note above `MAX_UPLOAD_CONTENT_BYTES`."""
    lines = content.splitlines()
    breadcrumb: list[str] = []
    sections: list[tuple[str, list[str]]] = [("", [])]

    for line in lines:
        match = _UPLOAD_HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            title = match.group(2).strip()
            breadcrumb = breadcrumb[: level - 1]
            while len(breadcrumb) < level - 1:
                breadcrumb.append("")
            breadcrumb.append(title)
            heading_path = " > ".join(b for b in breadcrumb if b)
            sections.append((heading_path, []))
            continue
        sections[-1][1].append(line)

    return [
        (heading, "\n".join(section_lines).strip())
        for heading, section_lines in sections
        if "\n".join(section_lines).strip()
    ]


def _chunk_upload_content(content: str, *, char_budget: int = UPLOAD_CHUNK_CHAR_BUDGET) -> list[dict]:
    """Chunk `content` into `{heading_path, chunk_index, content}` dicts:
    split on heading boundaries, then greedily pack each section's
    paragraphs (blank-line separated) into windows of at most `char_budget`
    characters, with no overlap. A single paragraph longer than
    `char_budget` becomes its own oversize chunk rather than being split
    mid-paragraph."""
    chunk_index = 0
    chunks: list[dict] = []
    for heading_path, section_text in _split_upload_into_heading_sections(content):
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", section_text) if p.strip()]
        if not paragraphs:
            continue

        window: list[str] = []
        window_len = 0
        for para in paragraphs:
            if window and window_len + len(para) > char_budget:
                chunks.append(
                    {
                        "heading_path": heading_path,
                        "chunk_index": chunk_index,
                        "content": "\n\n".join(window),
                    }
                )
                chunk_index += 1
                window = []
                window_len = 0
            window.append(para)
            window_len += len(para)
        if window:
            chunks.append(
                {
                    "heading_path": heading_path,
                    "chunk_index": chunk_index,
                    "content": "\n\n".join(window),
                }
            )
            chunk_index += 1

    return chunks


def upload_text(*, source: str, title: str, content: str) -> str:
    """Slugify `title`, chunk+embed `content`, and write it into an EXISTING
    `source_type='upload'` doc_sources row named `source` (by name, not id).

    Raises `UploadError` (never a raw exception) on:
      - `source` not found in `doc_sources`,
      - `source`'s `source_type != 'upload'` (this function NEVER writes into
        a crawl-type source, and never creates a `doc_sources` row — see the
        module note above `MAX_UPLOAD_CONTENT_BYTES`),
      - `content` exceeding `MAX_UPLOAD_CONTENT_BYTES` (measured in UTF-8
        bytes) or `title` exceeding `MAX_UPLOAD_TITLE_CHARS` (measured in
        characters) — either cap being exceeded rejects the WHOLE call with
        no partial processing.

    The page URL is the sentinel `upload://{source}/{slug}.md`, where `slug`
    is `title` slugified via `app.source_defaults._slugify` (the same
    slugify helper `derive_name` already uses elsewhere in this file) —
    re-uploading under the same `title` therefore replaces the same page
    (DELETE-then-INSERT, matching `ingestion/app/store.py`'s `replace_page`
    shape) rather than accumulating duplicates.
    """
    if len(title) > MAX_UPLOAD_TITLE_CHARS:
        raise UploadError(
            f"title is {len(title)} characters, exceeding the {MAX_UPLOAD_TITLE_CHARS}-character cap"
        )

    content_bytes = len(content.encode("utf-8"))
    if content_bytes > MAX_UPLOAD_CONTENT_BYTES:
        raise UploadError(
            f"content is {content_bytes} bytes, exceeding the {MAX_UPLOAD_CONTENT_BYTES}-byte (1 MB) cap"
        )

    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # The ONLY doc_sources access in this whole function: a read, by
            # name. There is no INSERT/UPDATE against doc_sources anywhere in
            # upload_text — see the module note above `MAX_UPLOAD_CONTENT_BYTES`.
            cur.execute(
                "SELECT id, source_type, language FROM doc_sources WHERE name = %s",
                (source,),
            )
            source_row = cur.fetchone()
            if source_row is None:
                raise UploadError(f"unknown source '{source}'")
            if source_row["source_type"] != "upload":
                raise UploadError(
                    f"source '{source}' does not accept uploads (source_type="
                    f"{source_row['source_type']!r}) — only a source_type='upload' source, "
                    "created by a human in the admin UI, accepts content through this tool"
                )
            source_id = source_row["id"]
            fts_config = source_row["language"]

            slug = _slugify(title) or "untitled"
            url = f"upload://{source}/{slug}.md"

            chunks = _chunk_upload_content(content)
            if not chunks:
                raise UploadError("content has no indexable text after chunking")

            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

            # Mirrors ingestion/app/store.py's `replace_page`: delete any
            # existing doc_pages row for this url (ON DELETE CASCADE wipes
            # its old chunks), then insert a fresh page + chunk rows.
            cur.execute("DELETE FROM doc_pages WHERE url = %s", (url,))
            cur.execute(
                """
                INSERT INTO doc_pages (source_id, url, content_hash)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (source_id, url, content_hash),
            )
            page_row = cur.fetchone()
            assert page_row is not None
            page_id = page_row["id"]

            for chunk in chunks:
                vector_literal = _embed_passage(chunk["content"])
                cur.execute(
                    """
                    INSERT INTO doc_chunks (page_id, heading_path, chunk_index, content, embedding, fts_config)
                    VALUES (%(page_id)s, %(heading_path)s, %(chunk_index)s, %(content)s,
                            %(embedding)s::vector, %(fts_config)s::regconfig)
                    """,
                    {
                        "page_id": page_id,
                        "heading_path": chunk["heading_path"],
                        "chunk_index": chunk["chunk_index"],
                        "content": chunk["content"],
                        "embedding": vector_literal,
                        "fts_config": fts_config,
                    },
                )

    logger.info("upload_doc_text", source=source, url=url, chunk_count=len(chunks))
    return f"Uploaded '{title}' to source '{source}' as {len(chunks)} chunk(s) (url={url})."
