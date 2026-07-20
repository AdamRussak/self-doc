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
import sys
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
