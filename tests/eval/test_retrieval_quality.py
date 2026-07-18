"""Retrieval quality evaluation harness.

Parametrized pytest suite that runs search queries against the live database
and asserts that expected snippets appear in the results. Tagged with
@pytest.mark.eval so it can be run independently via `make eval`.

Skips cleanly when no database is reachable (same pattern as existing
DB-dependent tests in the project).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Skip the entire module if the database is not reachable
# ---------------------------------------------------------------------------
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "127.0.0.1")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5433")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "self_docs")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "testpass123")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "self_docs")


def _db_is_reachable() -> bool:
    try:
        import psycopg

        conn = psycopg.connect(
            host=POSTGRES_HOST,
            port=int(POSTGRES_PORT),
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
            dbname=POSTGRES_DB,
            connect_timeout=3,
        )
        conn.close()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        not _db_is_reachable(),
        reason=f"Postgres not reachable at {POSTGRES_HOST}:{POSTGRES_PORT}",
    ),
]

# ---------------------------------------------------------------------------
# Load eval cases from YAML
# ---------------------------------------------------------------------------
EVAL_CASES_PATH = Path(__file__).parent / "eval_cases.yaml"


def _load_eval_cases() -> list[dict]:
    with open(EVAL_CASES_PATH) as f:
        data = yaml.safe_load(f)
    return data.get("cases", [])


EVAL_CASES = _load_eval_cases()

EXPECTED_SOURCES = sorted(
    {c["expected_source"] for c in EVAL_CASES if c.get("expected_source")}
)

# ---------------------------------------------------------------------------
# Retrieval helper — uses the retrieval module directly, not the MCP layer
# ---------------------------------------------------------------------------

# Patch env vars so retrieval.py can find the database
os.environ.setdefault("POSTGRES_HOST", POSTGRES_HOST)
os.environ.setdefault("POSTGRES_PORT", POSTGRES_PORT)
os.environ.setdefault("POSTGRES_USER", POSTGRES_USER)
os.environ.setdefault("POSTGRES_PASSWORD", POSTGRES_PASSWORD)
os.environ.setdefault("POSTGRES_DB", POSTGRES_DB)


def _chunk_counts_by_source(sources: list[str]) -> dict[str, int]:
    """Return {source_name: chunk_count} for each name in `sources`.

    A source with no `doc_sources` row at all (never synced) is counted as 0,
    same as a source with a row but zero `doc_chunks`.
    """
    import psycopg

    counts = {name: 0 for name in sources}
    conn = psycopg.connect(
        host=POSTGRES_HOST,
        port=int(POSTGRES_PORT),
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
        dbname=POSTGRES_DB,
        connect_timeout=5,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ds.name, COUNT(dc.*)
                FROM doc_sources ds
                LEFT JOIN doc_pages dp ON dp.source_id = ds.id
                LEFT JOIN doc_chunks dc ON dc.page_id = dp.id
                WHERE ds.name = ANY(%s)
                GROUP BY ds.name
                """,
                (sources,),
            )
            for name, count in cur.fetchall():
                counts[name] = count
    finally:
        conn.close()
    return counts


# Computed once at collection time (module scope), same lifetime as the
# DB-reachability check above. `EMPTY_SOURCES` is intentionally empty when the
# DB itself is unreachable (that path is handled entirely by `pytestmark`
# above, so this query must not run in that case).
EMPTY_SOURCES: dict[str, int] = {}
if EXPECTED_SOURCES and _db_is_reachable():
    _counts = _chunk_counts_by_source(EXPECTED_SOURCES)
    EMPTY_SOURCES = {name: n for name, n in _counts.items() if n == 0}


def test_corpus_precondition() -> None:
    """Fail loudly, ONCE, if any source referenced by an eval case has zero
    chunks indexed. This is a DATA/corpus problem, not a retrieval-ranking
    problem — a pile of opaque per-query assertion failures would obscure that
    distinction, so we surface it here as a single, clearly-labeled failure.
    Per-case tests for the affected sources are skipped (not failed) below,
    so they don't pile on top of this one.
    """
    if not EMPTY_SOURCES:
        return
    empty_desc = ", ".join(f"{name}={n} chunks" for name, n in sorted(EMPTY_SOURCES.items()))
    pytest.fail(
        "\n"
        + "=" * 78
        + "\nCORPUS INCOMPLETE — this is a DATA problem, not a retrieval-ranking bug.\n"
        + f"corpus incomplete: {empty_desc} — run 'make sync' before evaluating "
        "retrieval quality.\n"
        + "=" * 78,
        pytrace=False,
    )


def _search(query: str, source: str | None = None, limit: int = 5) -> str:
    """Lazy import to avoid import-time DB connection when skipping."""
    # Add mcp-server to path so we can import the retrieval module
    import sys

    mcp_server_path = str(Path(__file__).parent.parent.parent / "mcp-server")
    if mcp_server_path not in sys.path:
        sys.path.insert(0, mcp_server_path)

    from app.retrieval import search

    return search(query=query, source=source, limit=limit)


# ---------------------------------------------------------------------------
# Parametrized eval tests
# ---------------------------------------------------------------------------


@pytest.mark.eval
@pytest.mark.parametrize(
    "case",
    EVAL_CASES,
    ids=[c.get("query", f"case-{i}") for i, c in enumerate(EVAL_CASES)],
)
def test_eval_case(case: dict) -> None:
    """Assert that searching for `query` (optionally filtered by
    `expected_source`) returns at least `min_hits` results containing
    `expected_snippet` as a case-insensitive substring."""
    query = case["query"]
    source = case.get("expected_source")
    expected_snippet = case["expected_snippet"].lower()
    min_hits = case.get("min_hits", 1)

    if source in EMPTY_SOURCES:
        pytest.skip(
            f"corpus incomplete: source '{source}' has 0 chunks indexed — "
            f"see test_corpus_precondition; run 'make sync' first"
        )

    result = _search(query=query, source=source, limit=10)

    # Count how many result blocks contain the expected snippet
    # Results are separated by "\n\n---\n\n" per the retrieval contract
    if result == "No matching documentation found.":
        hits = 0
    else:
        blocks = result.split("\n\n---\n\n")
        hits = sum(1 for block in blocks if expected_snippet in block.lower())

    assert hits >= min_hits, (
        f"Expected ≥{min_hits} hits containing '{case['expected_snippet']}' "
        f"for query '{query}' (source={source}), got {hits}.\n"
        f"Result preview: {result[:500]}"
    )


# ---------------------------------------------------------------------------
# Summary fixture — print recall at the end of the session
# ---------------------------------------------------------------------------


# Map "<query>" test-id -> expected_source, used to attribute pass/fail
# results back to a source for the per-source recall breakdown below.
_CASE_SOURCE_BY_ID = {
    c.get("query", f"case-{i}"): c.get("expected_source")
    for i, c in enumerate(EVAL_CASES)
}


def _source_from_report(report) -> str | None:
    """Extract the expected_source for a test_eval_case report via its
    parametrize id, e.g. 'test_eval_case[FastAPI dependency injection]'."""
    nodeid = report.nodeid
    if "test_eval_case[" not in nodeid:
        return None
    case_id = nodeid.split("test_eval_case[", 1)[1].rsplit("]", 1)[0]
    return _CASE_SOURCE_BY_ID.get(case_id)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Print a recall summary after all eval tests, plus a per-source
    breakdown so a partial corpus is obvious at a glance."""
    passed = len(terminalreporter.stats.get("passed", []))
    failed = len(terminalreporter.stats.get("failed", []))
    total = passed + failed
    if total > 0:
        recall = passed / total * 100
        terminalreporter.write_line(
            f"\nRetrieval eval: {passed}/{total} cases passed ({recall:.0f}% recall)"
        )

    # Per-source recall: {source: [passed_count, total_count]}
    per_source: dict[str, list[int]] = {}
    for outcome in ("passed", "failed"):
        for report in terminalreporter.stats.get(outcome, []):
            source = _source_from_report(report)
            if source is None:
                continue
            counts = per_source.setdefault(source, [0, 0])
            counts[1] += 1
            if outcome == "passed":
                counts[0] += 1

    if per_source:
        terminalreporter.write_line("Retrieval eval — per-source recall:")
        for source in sorted(per_source):
            p, t = per_source[source]
            pct = (p / t * 100) if t else 0.0
            terminalreporter.write_line(f"  {source:20s} {p}/{t} passed ({pct:.0f}%)")

    if EMPTY_SOURCES:
        empty_desc = ", ".join(
            f"{name}={n} chunks" for name, n in sorted(EMPTY_SOURCES.items())
        )
        terminalreporter.write_line(
            f"\nCORPUS INCOMPLETE: {empty_desc} — see test_corpus_precondition "
            "failure above; results for these sources were skipped, not "
            "genuine ranking failures."
        )
