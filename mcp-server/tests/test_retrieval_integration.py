"""Integration tests for retrieval.py's hybrid RRF search against a live
Postgres (the compose `db` service, T1 schema).

Seeds known chunks with *real* `passage_embed` embeddings (same asymmetric
FastEmbed model used at ingest time) and exercises the real `search()`
entrypoint end-to-end through the hybrid RRF SQL:

  - an exact-token query (a rare literal string that appears in only one
    seeded chunk) must rank that chunk top via the FTS arm,
  - a semantically-paraphrased query with *no* lexical overlap with its
    target chunk must still surface that chunk via the vector arm,
  - the optional `source` filter must restrict results to the requested
    source only.

Skipped automatically (mirroring ingestion's test_store.py) when no live
Postgres is reachable on POSTGRES_HOST/PORT, so `pytest` stays green without
Docker (per-Spoke sandboxes, environments without the compose stack up).
"""

from __future__ import annotations

import os

import psycopg
import pytest
from fastembed import TextEmbedding
from psycopg.rows import dict_row

os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("POSTGRES_PORT", "5433")
os.environ.setdefault("POSTGRES_USER", "self_docs")
os.environ.setdefault("POSTGRES_PASSWORD", "testpass123")
os.environ.setdefault("POSTGRES_DB", "self_docs")

from app import retrieval  # noqa: E402 - env vars must be set before import


def _connect(**kwargs):
    return psycopg.connect(
        host=os.environ["POSTGRES_HOST"],
        port=os.environ["POSTGRES_PORT"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["POSTGRES_DB"],
        **kwargs,
    )


def _db_available() -> bool:
    try:
        _connect().close()
        return True
    except psycopg.OperationalError:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(), reason="no live Postgres reachable for retrieval.py integration tests"
)

# CHUNK_A carries a rare literal token that appears nowhere else — the FTS arm
# should find it via exact-token match and nothing else competes for it.
CHUNK_A = "Use zzqfrobnicate() to purge the internal cache index safely and quickly."
# CHUNK_B is themeable/UI content, worded so a paraphrase query shares no
# tokens with it but is semantically adjacent (dark mode / night theme).
CHUNK_B = (
    "The dark mode toggle lives inside the appearance section of the "
    "settings screen for this application."
)
# CHUNK_C carries a second rare literal token, seeded in a *different* source,
# used to prove the `source` filter actually restricts the candidate set.
CHUNK_C = "The wibblefratz utility runs a background compatibility scan during startup."


@pytest.fixture(scope="module")
def model() -> TextEmbedding:
    return TextEmbedding(model_name=retrieval.EMBEDDING_MODEL_NAME)


# The `seeded` fixture below only ever creates sources under these two
# names. Scoping cleanup to them (instead of wiping the whole doc_sources
# table) keeps this suite from ever touching genuine indexed sources
# (fastapi, traefik, docker-compose, pgvector-readme, ...) when run against
# the live DB.
_TEST_SOURCE_NAMES = ("source-a", "source-b")


def _purge_test_sources(c) -> None:
    # doc_pages/doc_chunks cascade from doc_sources via ON DELETE CASCADE, so
    # deleting the source row is sufficient to remove everything it owns.
    c.rollback()  # clear any aborted transaction left by a failing test
    with c.cursor() as cur:
        cur.execute("DELETE FROM doc_sources WHERE name = ANY(%s)", (list(_TEST_SOURCE_NAMES),))
    c.commit()


@pytest.fixture()
def conn():
    c = _connect(row_factory=dict_row)
    try:
        _purge_test_sources(c)  # safety net in case a prior run crashed mid-test
        yield c
    finally:
        _purge_test_sources(c)
        c.close()


class _NoCloseCtx:
    """`with pool.connection() as conn` context manager stand-in that yields
    an already-open connection without closing it on exit (the fixture owns
    the connection's lifecycle)."""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        return False


class _PoolShim:
    """Minimal stand-in for psycopg_pool.ConnectionPool exposing only the
    `.connection()` context manager that retrieval.search()/list_sources()
    use, so tests exercise the real search() function against the fixture's
    own connection instead of opening a second real pool."""

    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return _NoCloseCtx(self._conn)


def _insert_source(conn, name: str, base_url: str = "https://example.com/") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO doc_sources (name, base_url) VALUES (%s, %s) RETURNING id",
            (name, base_url),
        )
        row = cur.fetchone()
    conn.commit()
    return row["id"]


def _insert_chunk(conn, model: TextEmbedding, source_id: int, url: str, heading_path: str, content: str) -> None:
    (vec,) = list(model.passage_embed([content]))
    literal = retrieval._format_vector_literal(vec)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO doc_pages (source_id, url, content_hash) VALUES (%s, %s, %s) RETURNING id",
            (source_id, url, "d" * 64),
        )
        page_id = cur.fetchone()["id"]
        cur.execute(
            """
            INSERT INTO doc_chunks (page_id, heading_path, chunk_index, content, embedding)
            VALUES (%s, %s, 0, %s, %s::vector)
            """,
            (page_id, heading_path, content, literal),
        )
    conn.commit()


@pytest.fixture()
def seeded(conn, model, monkeypatch):
    monkeypatch.setattr(retrieval, "get_pool", lambda: _PoolShim(conn))
    monkeypatch.setattr(retrieval, "get_embedding_model", lambda: model)

    source_a = _insert_source(conn, "source-a")
    source_b = _insert_source(conn, "source-b")
    _insert_chunk(conn, model, source_a, "https://example.com/a-cache", "Guide > Cache", CHUNK_A)
    _insert_chunk(conn, model, source_a, "https://example.com/a-theme", "UI > Appearance", CHUNK_B)
    _insert_chunk(conn, model, source_b, "https://example.com/b-wibble", "Ops > Startup", CHUNK_C)
    return conn


def test_exact_token_query_ranks_its_chunk_top_via_fts_arm(seeded):
    # Scoped to source-a: the live DB also holds the real indexed corpus
    # (thousands of unrelated chunks) alongside the seeded fixture data, so
    # an unscoped search could in principle surface an unrelated real chunk
    # instead of the one this test seeded and cares about.
    result = retrieval.search("zzqfrobnicate", source="source-a", limit=5)
    top_hit = result.split("\n\n---\n\n")[0]
    assert "a-cache" in top_hit


def test_paraphrase_query_hits_its_chunk_via_vector_arm(seeded):
    # No lexical overlap with CHUNK_B's wording ("dark mode", "appearance",
    # "settings", "toggle") — only the vector arm can surface this. Scoped to
    # source-a for the same isolation reason as above: the vector arm has no
    # relevance floor, so an unscoped query can be outranked by a real corpus
    # chunk that is coincidentally closer in embedding space.
    result = retrieval.search("how do I switch the interface to a night theme", source="source-a", limit=5)
    top_hit = result.split("\n\n---\n\n")[0]
    assert "a-theme" in top_hit


def test_source_filter_restricts_results(seeded):
    # "wibblefratz" only exists in source-b's chunk. Filtering to source-a
    # must exclude that chunk entirely from the results even though the FTS
    # arm would otherwise surface it (the vector arm still returns *some*
    # nearest-neighbor candidates from source-a since cosine search has no
    # relevance floor — the assertion is on exclusion, not emptiness).
    result_wrong_source = retrieval.search("wibblefratz", source="source-a", limit=5)
    assert "b-wibble" not in result_wrong_source
    assert "wibblefratz" not in result_wrong_source

    result_right_source = retrieval.search("wibblefratz", source="source-b", limit=5)
    assert "b-wibble" in result_right_source
