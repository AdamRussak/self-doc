"""End-to-end test spanning both packages against the real compose Postgres.

Serves a tiny fixture doc site (static HTML, 3 pages with planted, distinctive
sentences) via `python -m http.server` on localhost, then:

  1. runs the REAL ingestion sync path (`app.store.sync_source`, in the
     ingestion package's own venv/interpreter — crawl -> extract -> chunk ->
     embed -> upsert) against that fixture site;
  2. runs the REAL mcp-server search path (`app.retrieval.search`, in the
     mcp-server package's own venv/interpreter — query_embed + hybrid RRF SQL)
     against the resulting rows.

The two packages both ship a top-level `app` package, so each half runs as a
subprocess using that package's own interpreter/venv rather than importing
both into this test's process (which would collide on `sys.modules["app"]`).
This is still an in-process-per-package, no-Docker-network-required e2e test
per the T8 acceptance criteria — only the fixture HTTP server and the compose
`db` are external.

Skipped cleanly (matching ingestion's test_store.py / mcp-server's
test_retrieval_integration.py) when the compose db isn't reachable, or when
either package's venv hasn't been created yet (see Makefile `test` target,
which creates both before running suites).
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INGESTION_ROOT = REPO_ROOT / "ingestion"
MCP_ROOT = REPO_ROOT / "mcp-server"
INGESTION_PY = INGESTION_ROOT / ".venv" / "bin" / "python"
MCP_PY = MCP_ROOT / ".venv" / "bin" / "python"

PG_ENV = {
    "POSTGRES_HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
    "POSTGRES_PORT": os.environ.get("POSTGRES_PORT", "5433"),
    "POSTGRES_USER": os.environ.get("POSTGRES_USER", "self_docs"),
    "POSTGRES_PASSWORD": os.environ.get("POSTGRES_PASSWORD", "testpass123"),
    "POSTGRES_DB": os.environ.get("POSTGRES_DB", "self_docs"),
}


def _db_available() -> bool:
    try:
        conn = psycopg.connect(
            host=PG_ENV["POSTGRES_HOST"],
            port=PG_ENV["POSTGRES_PORT"],
            user=PG_ENV["POSTGRES_USER"],
            password=PG_ENV["POSTGRES_PASSWORD"],
            dbname=PG_ENV["POSTGRES_DB"],
        )
        conn.close()
        return True
    except psycopg.OperationalError:
        return False


pytestmark = [
    pytest.mark.skipif(not _db_available(), reason="no live Postgres reachable for e2e test"),
    pytest.mark.skipif(
        not (INGESTION_PY.exists() and MCP_PY.exists()),
        reason="ingestion/.venv or mcp-server/.venv not set up (run `make test` from repo root)",
    ),
]

SOURCE_NAME = "e2e-mini-site"

PLANTED_A = (
    "The frobnicator widget requires a calibration cycle every ninety days to "
    "maintain accuracy and prevent drift in the output torque readings across "
    "all standard operating temperatures."
)
PLANTED_B = (
    "Photon capacitors must be fully discharged inside a grounded chamber for "
    "at least ten minutes before the unit is packaged for shipping, to avoid "
    "residual charge hazards during transit."
)

INDEX_HTML = """<html><body>
<article>
<h1>Fixture Docs Home</h1>
<p>Welcome to the fictional Frobnicator product documentation, covering
routine hardware maintenance procedures and photon capacitor safety handling
guidance for field service technicians working on this equipment line.</p>
<a href="/page1.html">Frobnicator Maintenance</a>
<a href="/page2.html">Photon Capacitor Safety</a>
</article>
</body></html>
"""

PAGE1_HTML = f"""<html><body>
<nav>Skip this nav content entirely please</nav>
<article>
<h1>Frobnicator Maintenance</h1>
<h2>Calibration</h2>
<p>{PLANTED_A}</p>
<p>Technicians should log each calibration cycle in the maintenance ledger
along with the ambient temperature and the torque reading observed at the
time of service, to help correlate any future drift with prior sessions.</p>
</article>
<footer>Skip this footer content entirely please</footer>
</body></html>
"""

PAGE2_HTML = f"""<html><body>
<nav>Skip this nav content entirely please</nav>
<article>
<h1>Photon Capacitor Safety</h1>
<h2>Shipping preparation</h2>
<p>{PLANTED_B}</p>
<p>Failure to fully discharge the capacitor bank before transit has been
linked to intermittent static discharge events during unpacking, so this
step is mandatory for every outbound unit regardless of destination.</p>
</article>
<footer>Skip this footer content entirely please</footer>
</body></html>
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def mini_site(tmp_path_factory):
    site_dir = tmp_path_factory.mktemp("mini_site")
    (site_dir / "index.html").write_text(INDEX_HTML)
    (site_dir / "page1.html").write_text(PAGE1_HTML)
    (site_dir / "page2.html").write_text(PAGE2_HTML)

    port = _free_port()
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=str(site_dir), **kw
    )
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}/"
    # Wait for the server to actually accept connections before handing back.
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)

    yield base_url

    httpd.shutdown()
    thread.join(timeout=5)


@pytest.fixture()
def clean_source():
    """Remove any leftover e2e-mini-site rows before the test (idempotent
    reruns) and clean up afterwards."""

    def _delete():
        conn = psycopg.connect(
            host=PG_ENV["POSTGRES_HOST"],
            port=PG_ENV["POSTGRES_PORT"],
            user=PG_ENV["POSTGRES_USER"],
            password=PG_ENV["POSTGRES_PASSWORD"],
            dbname=PG_ENV["POSTGRES_DB"],
        )
        with conn.cursor() as cur:
            cur.execute("DELETE FROM doc_sources WHERE name = %s", (SOURCE_NAME,))
        conn.commit()
        conn.close()

    _delete()
    yield
    _delete()


def _run_ingestion_sync(base_url: str) -> dict:
    """Run the real crawl -> extract -> chunk -> embed -> store pipeline
    (`app.store.sync_source`) in the ingestion package's own venv."""
    script = f"""
import json
from app.config import SourceConfig
from app import store

source = SourceConfig(
    name={SOURCE_NAME!r},
    base_url={base_url!r},
    max_pages=10,
    rate_limit_rps=1000,
)
conn = store.get_connection()
try:
    outcome = store.sync_source(source, conn)
finally:
    conn.close()
print(json.dumps({{
    "status": outcome.status,
    "pages_fetched": outcome.pages_fetched,
    "pages_failed": outcome.pages_failed,
    "pages_soft_failed": outcome.pages_soft_failed,
    "chunks_indexed": outcome.chunks_indexed,
    "error": outcome.error,
}}))
"""
    env = os.environ.copy()
    env.update(PG_ENV)
    env["SELF_DOCS_ALLOW_PRIVATE_ADDRESSES"] = "1"
    proc = subprocess.run(
        [str(INGESTION_PY), "-c", script],
        cwd=str(INGESTION_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"ingestion sync subprocess failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _run_search(query: str) -> str:
    """Run the real query_embed + hybrid RRF search (`app.retrieval.search`)
    in the mcp-server package's own venv."""
    script = f"""
from app import retrieval
result = retrieval.search({query!r}, source={SOURCE_NAME!r}, limit=5)
print(result)
"""
    env = os.environ.copy()
    env.update(PG_ENV)
    env["SELF_DOCS_ALLOW_PRIVATE_ADDRESSES"] = "1"
    proc = subprocess.run(
        [str(MCP_PY), "-c", script],
        cwd=str(MCP_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"mcp-server search subprocess failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


def test_crawl_sync_embed_store_search_round_trip(mini_site, clean_source):
    outcome = _run_ingestion_sync(mini_site)
    assert outcome["status"] == "ok", outcome
    assert outcome["chunks_indexed"] > 0
    assert outcome["pages_fetched"] >= 2  # at least page1 + page2 indexed

    result = _run_search("frobnicator calibration cycle torque readings")
    assert f"{mini_site}page1.html" in result
    assert "ninety days" in result


def test_search_finds_second_planted_page(mini_site, clean_source):
    _run_ingestion_sync(mini_site)

    result = _run_search("discharging photon capacitors before shipping")
    assert f"{mini_site}page2.html" in result


# --- Upload pipeline e2e (T17) ----------------------------------------------
#
# Exercises the whole admin-upload pipeline (T1-T13) as one real, DB-backed
# flow rather than isolated unit tests: `upload_zip.expand_zip` (T5) ->
# `store.ingest_uploaded_docs` (T6) -> `store.search_chunks` (the same
# hybrid-RRF search `sync_source`-indexed content uses) -> `mcp-server`'s
# `retrieval.search` (T-whatever wires the MCP tool) -> `store.purge_source`
# cascade-delete. Same subprocess-per-package pattern as the crawl e2e tests
# above (`_run_ingestion_sync`/`_run_search`) and for the identical reason:
# both packages ship a top-level `app` module that would collide in
# `sys.modules` if imported directly into this test process.
#
# This test operates at the `sources_repo`/`store` layer (direct Python
# calls to the same functions the admin upload route (T9) calls), not the
# admin HTTP route itself — matching `ingestion/tests/test_store.py`'s
# `make_upload_source` helper convention, which is the closest existing
# precedent in this repo for constructing an upload source under test. The
# real chunker and real embedder run in-process in the ingestion venv
# subprocess (no mocking of the embedding model), so the mcp-server vector
# search assertion below is genuine end-to-end proof, not a placeholder.

UPLOAD_SOURCE_NAME = "e2e-upload-bundle"

UPLOAD_MD_A = """# Gralvorine Subassembly

The gralvorine subassembly must be torque-tested at eleven newton meters
before final packaging, per revision four of the assembly specification,
and any deviation greater than half a newton meter should be logged in the
quality tracking system for later review by the calibration team.

## Notes

Additional notes about routine handling procedures for this subassembly
during standard warehouse storage conditions, covering humidity and
temperature ranges that keep the components within tolerance over time.
"""

UPLOAD_MD_B = """# Marmoset Calibration

Quixotic marmoset calibration requires realigning the tertiary flux
capacitor housing within a two millimeter tolerance window, performed only
by technicians who have completed the advanced realignment certification
course offered twice yearly at the regional training center.

## Follow-up

Technicians should record the realignment offset in the calibration log
immediately afterward, noting the ambient humidity and any unusual
vibration patterns observed during the adjustment procedure itself.
"""

UPLOAD_HTML_C = """<html><body>
<nav>Skip this nav content entirely please</nav>
<article>
<h1>Splendifera Array Maintenance</h1>
<h2>Defragmentation</h2>
<p>Nocturnal splendifera arrays should be defragged every leap year to
avoid entropic drift in the secondary lattice, a process that typically
takes several hours and should only be performed during scheduled
maintenance windows to avoid disrupting active data pathways.</p>
<p>Field technicians should verify the lattice checksum before and after
defragmentation to confirm no data corruption occurred during the
realignment process, logging the results in the maintenance ledger.</p>
</article>
<footer>Skip this footer content entirely please</footer>
</body></html>
"""


@pytest.fixture()
def clean_upload_source():
    """Remove any leftover e2e-upload-bundle rows before the test (idempotent
    reruns) and clean up afterwards. Deleting the `doc_sources` row cascades
    to `doc_pages`/`doc_chunks`, which is also what makes back-to-back runs
    of this test not accumulate data even though the test's own explicit
    `purge_source` step only clears pages/chunks, not the source row."""

    def _delete():
        conn = psycopg.connect(
            host=PG_ENV["POSTGRES_HOST"],
            port=PG_ENV["POSTGRES_PORT"],
            user=PG_ENV["POSTGRES_USER"],
            password=PG_ENV["POSTGRES_PASSWORD"],
            dbname=PG_ENV["POSTGRES_DB"],
        )
        with conn.cursor() as cur:
            cur.execute("DELETE FROM doc_sources WHERE name = %s", (UPLOAD_SOURCE_NAME,))
        conn.commit()
        conn.close()

    _delete()
    yield
    _delete()


def _run_ingestion_upload_and_search(query: str) -> dict:
    """Build an in-memory zip (2 markdown members + 1 HTML member), create a
    `source_type='upload'` source via `sources_repo.create_source`, expand
    the zip via `upload_zip.expand_zip` and index it via
    `store.ingest_uploaded_docs` (the exact T5/T6 call shapes the admin
    upload route uses), then re-run the identical ingest a second time to
    prove idempotent re-ingest (`pages_skipped` on every page). Runs in the
    ingestion package's own venv/interpreter."""
    script = f"""
import io
import json
import zipfile

from app import sources_repo, store, upload_zip
from app.config import SourceConfig

SOURCE_NAME = {UPLOAD_SOURCE_NAME!r}

zip_buf = io.BytesIO()
with zipfile.ZipFile(zip_buf, "w") as zf:
    zf.writestr("gralvorine.md", {UPLOAD_MD_A!r})
    zf.writestr("nested/marmoset.md", {UPLOAD_MD_B!r})
    zf.writestr("splendifera.html", {UPLOAD_HTML_C!r})
zip_bytes = zip_buf.getvalue()

cfg = SourceConfig.model_validate({{
    "name": SOURCE_NAME,
    "source_type": "upload",
    "base_url": f"upload://{{SOURCE_NAME}}",
}})
conn = store.get_connection()
try:
    source_id = sources_repo.create_source(conn, cfg)
    source = sources_repo.get_source(conn, source_id)

    expansion1 = upload_zip.expand_zip("bundle.zip", zip_bytes)
    assert not expansion1.failures, expansion1.failures
    assert not expansion1.skipped, expansion1.skipped
    assert len(expansion1.docs) == 3, [d.rel_path for d in expansion1.docs]

    outcome1 = store.ingest_uploaded_docs(conn, source, expansion1.docs)

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM doc_pages WHERE source_id = %s", (source_id,))
        (page_count,) = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) FROM doc_chunks c JOIN doc_pages p ON c.page_id = p.id "
            "WHERE p.source_id = %s",
            (source_id,),
        )
        (chunk_count,) = cur.fetchone()

    search_results = store.search_chunks(conn, {query!r}, source=SOURCE_NAME, limit=5)
    search_urls = [r["url"] for r in search_results]

    # Re-parse and re-ingest the IDENTICAL zip bytes a second time: proves
    # idempotent re-ingest (content_hash unchanged -> every page skipped).
    expansion2 = upload_zip.expand_zip("bundle.zip", zip_bytes)
    outcome2 = store.ingest_uploaded_docs(conn, source, expansion2.docs)

    print(json.dumps({{
        "source_id": source_id,
        "outcome1_status": outcome1.status,
        "outcome1_pages_fetched": outcome1.pages_fetched,
        "outcome1_pages_skipped": outcome1.pages_skipped,
        "outcome1_pages_failed": outcome1.pages_failed,
        "outcome1_chunks_indexed": outcome1.chunks_indexed,
        "page_count": page_count,
        "chunk_count": chunk_count,
        "search_urls": search_urls,
        "outcome2_status": outcome2.status,
        "outcome2_pages_fetched": outcome2.pages_fetched,
        "outcome2_pages_skipped": outcome2.pages_skipped,
        "outcome2_pages_failed": outcome2.pages_failed,
    }}))
finally:
    conn.close()
"""
    env = os.environ.copy()
    env.update(PG_ENV)
    proc = subprocess.run(
        [str(INGESTION_PY), "-c", script],
        cwd=str(INGESTION_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"ingestion upload subprocess failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _run_purge_upload_source(source_id: int) -> dict:
    """Call `store.purge_source` on the upload source and report the
    remaining page/chunk counts, proving cascade delete works for upload
    sources exactly as it does for crawl sources. Runs in the ingestion
    package's own venv/interpreter."""
    script = f"""
import json

from app import store

source_id = {source_id!r}
conn = store.get_connection()
try:
    deleted = store.purge_source(conn, source_id)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM doc_pages WHERE source_id = %s", (source_id,))
        (page_count,) = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) FROM doc_chunks c JOIN doc_pages p ON c.page_id = p.id "
            "WHERE p.source_id = %s",
            (source_id,),
        )
        (chunk_count,) = cur.fetchone()
    print(json.dumps({{"deleted": deleted, "page_count": page_count, "chunk_count": chunk_count}}))
finally:
    conn.close()
"""
    env = os.environ.copy()
    env.update(PG_ENV)
    proc = subprocess.run(
        [str(INGESTION_PY), "-c", script],
        cwd=str(INGESTION_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"ingestion purge subprocess failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _run_mcp_search(query: str, *, source: str) -> str:
    """Run the real `app.retrieval.search` (hybrid RRF, scoped by `source`)
    in the mcp-server package's own venv/interpreter."""
    script = f"""
from app import retrieval
result = retrieval.search({query!r}, source={source!r}, limit=5)
print(result)
"""
    env = os.environ.copy()
    env.update(PG_ENV)
    proc = subprocess.run(
        [str(MCP_PY), "-c", script],
        cwd=str(MCP_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"mcp-server search subprocess failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


def test_upload_zip_ingest_search_reingest_purge_round_trip(clean_upload_source):
    # Steps 1-4: create the upload source, expand an in-memory zip (2 md +
    # 1 html member, distinct greppable content each), ingest it via the
    # same `store.ingest_uploaded_docs` call shape the admin route uses, and
    # assert exactly 3 pages / >0 chunks landed.
    result1 = _run_ingestion_upload_and_search("quixotic marmoset flux capacitor calibration")

    assert result1["outcome1_status"] == "ok", result1
    assert result1["outcome1_pages_fetched"] == 3
    assert result1["outcome1_pages_skipped"] == 0
    assert result1["outcome1_pages_failed"] == 0
    assert result1["outcome1_chunks_indexed"] > 0
    assert result1["page_count"] == 3
    assert result1["chunk_count"] > 0

    # Step 5: `store.search_chunks` finds text unique to one zip member
    # (marmoset.md) — proves real content landed, not placeholder rows.
    assert any(
        url == f"upload://{UPLOAD_SOURCE_NAME}/nested/marmoset.md" for url in result1["search_urls"]
    ), result1["search_urls"]

    # Step 7: re-ingesting the IDENTICAL zip bytes skips all 3 pages
    # (content_hash unchanged) — idempotent re-ingest.
    assert result1["outcome2_status"] == "ok", result1
    assert result1["outcome2_pages_fetched"] == 0
    assert result1["outcome2_pages_skipped"] == 3
    assert result1["outcome2_pages_failed"] == 0

    # Step 6: the same planted text is retrievable through mcp-server's real
    # `retrieval.search`, scoped with the `source` filter, using the pinned
    # test embedder — proves the whole pipeline round-trips into a live
    # vector/FTS search a real MCP client would issue.
    mcp_result = _run_mcp_search(
        "quixotic marmoset flux capacitor calibration", source=UPLOAD_SOURCE_NAME
    )
    assert "marmoset" in mcp_result.lower()
    assert f"upload://{UPLOAD_SOURCE_NAME}/nested/marmoset.md" in mcp_result

    # Step 8: `store.purge_source` deletes the pages/chunks (cascade delete
    # working for upload sources exactly as it does for crawl sources).
    purge_result = _run_purge_upload_source(result1["source_id"])
    assert purge_result["deleted"] == 3
    assert purge_result["page_count"] == 0
    assert purge_result["chunk_count"] == 0
