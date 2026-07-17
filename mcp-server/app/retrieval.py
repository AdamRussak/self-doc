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
import os
import time
from typing import Any

import structlog
from fastembed import TextEmbedding
from prometheus_client import Counter, Histogram
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

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
