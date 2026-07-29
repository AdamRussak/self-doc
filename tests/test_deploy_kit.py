"""Guards that keep the `deploy/` standalone install kit faithful to its
contracts: `deploy/models.tsv` must be an exact, byte-identical render of
`scripts/models_matrix.py --format tsv` (so editing `config/models.yaml`
without running `make deploy-manifest` fails CI, mirroring the existing
`01_schema.sql` parity guard in `tests/test_model_registry.py`); the two
documented pipe-delimited parser hazards (a significant trailing space on
some `query_prompt` values, and an empty `passage_prompt` on most rows) must
survive into the committed file; `deploy/install.sh` must render a correct,
0600 `.env` and a dimension-complete `01_schema.sql` for every registry model
via `--dry-run` (zero Docker calls), generate two distinct secrets that both
fail the real `is_placeholder_token` boot-time check, refuse a re-run without
`--force`, and leave an untouched `.env` when it does; and
`deploy/docker-compose.yml` must stay build-free, profile-free,
loopback-only, and scoped to exactly its documented 16-variable contract.

Sections 10-12 lock down a follow-up security-review fix landing from a
parallel Spoke in this same worktree: (10) every field parsed out of
models.tsv must be validated immediately after parsing — non-numeric/empty/
sed-metacharacter `dim`, a hostile `slug`, and a `"`-bearing prompt must each
exit 2 before any of that value reaches a rendered file, tested against a
SYNTHETIC models.tsv (the committed one is never touched), plus a negative
control that the real committed manifest still validates cleanly; (11) an
option value beginning with `-` (e.g. `--dir --force`) must be rejected with
a clean usage error, not silently consumed; (12) a `--dir` crafted to break
out of `check_disk_space`'s unquoted awk `BEGIN{}` splice must never execute.
Until the parallel fix lands, the tests in these sections marked "EXPECTED
RED pre-fix" fail against today's install.sh — each docstring records the
exact reproduction verified by hand in this worktree, so the flip to green
at integration is checkable test-by-test.

Sections 13-14 lock down a second, later round of fixes (also landing in
parallel, on `wt/U3`/`wt/INT`, not yet on this branch): (13) `fetch_file`'s
mktemp(0600)->mv left db/init/02-04*.sql at mode 0600 while 01_schema.sql
(written by shell redirection under the ambient umask) landed at 0644 — an
asymmetry that is invisible on macOS but leaves postgres (a non-root,
non-owning uid on Linux) unable to read 3 of 4 init scripts, silently
half-seeding the database on first boot; (14) `--force` used to
unconditionally regenerate POSTGRES_PASSWORD even though the `pgdata` Docker
volume survives a plain `down`, and Postgres only ever applies
POSTGRES_PASSWORD while initializing an EMPTY volume — so a `--force` re-run
against a surviving volume silently locked the operator out of their own
already-indexed corpus. The fix preserves POSTGRES_PASSWORD whenever the
volume's existence can't be conclusively ruled out (including the entirety
of --dry-run, where no docker calls are made at all) and only generates a
fresh one when `docker volume inspect` confirms the volume is actually gone.

Section 15 locks down a follow-up to section 14's own fix: nothing checked
that a --force'd model CHANGE still matched what a surviving, confirmed-
present pgdata volume was initialized for — Postgres never re-applies
db/init to a non-empty volume, so a silently-accepted model/dimension change
there leaves the live column at the OLD dimension while the image label,
.env, and rendered schema all move to the NEW one. The fix refuses (exit 2)
whenever the newly selected model/dim disagrees with the OLD .env's, or when
the old password/model-name can't be recovered from .env at all, in both
cases naming `docker compose down -v` as the deliberate destructive path.

Section 16 locks down a further follow-up: db/init/01_schema.sql was
(re-)rendered BEFORE section 15's model-match refusal ran, so a REFUSED
--force still left 01_schema.sql on disk rewritten for the rejected model —
not inert, since the refusal message's own recommended recovery path
(`docker compose down -v` + `docker compose up -d`) applies db/init fresh to
the resulting empty volume, baking in the rejected dimension for real. The
fix moves the schema render below the volume/model check so a refusal
leaves the on-disk schema untouched; the docker-compose.yml fetch
deliberately stays where it is (model-independent, and volume detection
needs it already fetched).

Runs under the ingestion venv (see Makefile's `test` target), so PyYAML is
available and `ingestion/app/security.py` (imported directly, standalone) can
be loaded without needing the mcp-server package's own `app`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from types import ModuleType

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "config" / "models.yaml"
MATRIX_SCRIPT = REPO_ROOT / "scripts" / "models_matrix.py"
DEPLOY_DIR = REPO_ROOT / "deploy"
MODELS_TSV = DEPLOY_DIR / "models.tsv"
COMPOSE_FILE = DEPLOY_DIR / "docker-compose.yml"
INSTALL_SH = DEPLOY_DIR / "install.sh"
SECURITY_PY = REPO_ROOT / "ingestion" / "app" / "security.py"

TSV_FIELDS = (
    "slug",
    "model",
    "dim",
    "mem_ingestion",
    "mem_mcp",
    "query_prompt",
    "passage_prompt",
    "is_default",
)

# The docker-compose.yml header comment's documented variable contract (16
# variables, see its own docstring) — the sole set of names it may reference.
CONTRACT_VARS = frozenset(
    {
        "SELF_DOCS_OWNER",
        "SELF_DOCS_IMAGE_TAG",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "SYNC_TOKEN",
        "MCP_TOKEN",
        "EMBEDDING_MODEL_NAME",
        "EMBEDDING_DIM",
        "EMBEDDING_QUERY_PROMPT",
        "EMBEDDING_PASSAGE_PROMPT",
        "INGESTION_MEM_LIMIT",
        "MCP_MEM_LIMIT",
        "SELF_DOCS_API_PORT",
        "SELF_DOCS_MCP_PORT",
        "TZ",
    }
)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec: security.py's frozen dataclass has
    # `from __future__ import annotations` (string annotations), and
    # dataclasses resolves those via `sys.modules[cls.__module__]` at class
    # body execution time — without this, that lookup returns None and
    # dataclass() crashes on a module that imported clean everywhere else.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_models_matrix() -> ModuleType:
    return _load_module("models_matrix", MATRIX_SCRIPT)


def _load_security() -> ModuleType:
    # security.py imports only stdlib (ipaddress/os/dataclasses/urllib.parse),
    # so it can be loaded standalone without the ingestion package's `app`
    # context (and without colliding with mcp-server's own `app` package —
    # see tests/test_e2e.py's docstring for why that collision matters).
    return _load_module("self_docs_security", SECURITY_PY)


def _load_registry() -> dict:
    with REGISTRY_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _split_tsv_line(line: str) -> dict[str, str]:
    """Split one pipe-delimited line into the documented field order, WITHOUT
    collapsing adjacent delimiters (str.split("|") never collapses, unlike
    awk -F'|'/cut -s/tr -s — see module docstring)."""
    parts = line.split("|")
    assert len(parts) == len(TSV_FIELDS), f"line has {len(parts)} fields, expected {len(TSV_FIELDS)}: {line!r}"
    return dict(zip(TSV_FIELDS, parts, strict=True))


mm = _load_models_matrix()
REGISTRY = _load_registry()


def _committed_tsv_rows() -> list[dict[str, str]]:
    text = MODELS_TSV.read_text(encoding="utf-8")
    lines = text.split("\n")
    assert lines[-1] == "", "deploy/models.tsv must end with a trailing newline"
    lines = lines[:-1]
    return [_split_tsv_line(line) for line in lines]


# ---------------------------------------------------------------------------
# 1. PARITY: committed deploy/models.tsv == fresh `--format tsv` render.
# ---------------------------------------------------------------------------


def test_committed_tsv_is_byte_identical_to_fresh_generator_output():
    """Locks the `make deploy-manifest` drift: editing config/models.yaml
    without re-running `make deploy-manifest` must fail this test, exactly
    like tests/test_model_registry.py's schema-parity guard."""
    result = subprocess.run(
        [sys.executable, str(MATRIX_SCRIPT), "--format", "tsv"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    committed = MODELS_TSV.read_text(encoding="utf-8")
    assert result.stdout == committed


# ---------------------------------------------------------------------------
# 2. Every TSV row agrees with the JSON matrix and config/models.yaml.
# ---------------------------------------------------------------------------


def test_tsv_row_count_matches_registry_model_count():
    rows = _committed_tsv_rows()
    assert len(rows) == len(REGISTRY["models"])


@pytest.mark.parametrize("row", _committed_tsv_rows(), ids=lambda r: r["slug"])
def test_tsv_row_slug_model_dim_agree_with_json_matrix_and_registry(row):
    json_matrix = mm.build_matrix(REGISTRY, only=None)
    json_row = next(r for r in json_matrix if r["slug"] == row["slug"])
    assert row["model"] == json_row["model"]
    assert int(row["dim"]) == json_row["dim"]
    assert (row["is_default"] == "true") == json_row["is_default"]

    registry_row = REGISTRY["models"][row["model"]]
    assert int(row["dim"]) == registry_row["dim"]
    assert row["mem_ingestion"] == str(registry_row["mem_ingestion"])
    assert row["mem_mcp"] == str(registry_row["mem_mcp"])
    assert row["query_prompt"] == registry_row["query_prompt"]
    assert row["passage_prompt"] == registry_row["passage_prompt"]


def test_exactly_one_tsv_row_is_default_and_it_sorts_first():
    rows = _committed_tsv_rows()
    defaults = [r for r in rows if r["is_default"] == "true"]
    assert len(defaults) == 1
    assert defaults[0]["model"] == REGISTRY["default"]
    assert rows[0]["model"] == REGISTRY["default"]


# ---------------------------------------------------------------------------
# 3. Trailing-space parser hazard: query_prompt's significant trailing space
# must survive into the committed file.
# ---------------------------------------------------------------------------


def test_query_prompt_trailing_space_survives_in_committed_tsv():
    rows = {r["slug"]: r for r in _committed_tsv_rows()}
    for slug in ("bge-small-en-v1.5", "bge-base-en-v1.5", "mxbai-embed-large-v1"):
        prompt = rows[slug]["query_prompt"]
        assert prompt.endswith(" "), f"{slug}'s query_prompt lost its trailing space: {prompt!r}"
        assert prompt == "Represent this sentence for searching relevant passages: "


def test_e5_query_and_passage_prompts_have_no_trailing_space_and_are_distinct_from_bge():
    # Regression guard for the OTHER shape: multilingual-e5-large's prompts
    # are short prefixes with a trailing space of their own ("query: ",
    # "passage: ") — different text from the bge/mxbai family, so a copy-paste
    # bug in the registry can't hide behind a shared assertion.
    row = next(r for r in _committed_tsv_rows() if r["slug"] == "multilingual-e5-large")
    assert row["query_prompt"] == "query: "
    assert row["passage_prompt"] == "passage: "


# ---------------------------------------------------------------------------
# 4. Adjacent-pipe hazard: empty passage_prompt on 3/4 rows must not collapse
# fields; no field may contain the delimiter; trailing newline present.
# ---------------------------------------------------------------------------


def test_passage_prompt_is_empty_on_three_of_four_rows_without_misaligning_fields():
    rows = _committed_tsv_rows()
    empty_passage = [r for r in rows if r["passage_prompt"] == ""]
    assert len(empty_passage) == 3
    # Misalignment would smear is_default's "true"/"false" into passage_prompt
    # or vice versa — assert every row's is_default is still a clean boolean
    # string, proving the adjacent "||" pair didn't swallow a neighbor field.
    for row in rows:
        assert row["is_default"] in ("true", "false")


def test_no_committed_field_contains_the_pipe_delimiter_or_a_newline():
    for row in _committed_tsv_rows():
        for field, value in row.items():
            assert "|" not in value, f"field {field!r} contains '|': {value!r}"
            assert "\n" not in value, f"field {field!r} contains a newline: {value!r}"


def test_committed_tsv_ends_with_trailing_newline():
    # A missing trailing newline silently drops the last model from the
    # installer's `while IFS='|' read -r ... ; done < "$MODELS_TSV"` loop.
    raw = MODELS_TSV.read_bytes()
    assert raw.endswith(b"\n")


# ---------------------------------------------------------------------------
# 5. install.sh syntax.
# ---------------------------------------------------------------------------


def test_install_sh_has_clean_bash_syntax():
    result = subprocess.run(
        ["bash", "-n", str(INSTALL_SH)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# 6. Per-model dry-run: no Docker required.
# ---------------------------------------------------------------------------


def _all_slugs() -> list[str]:
    return [row["slug"] for row in _committed_tsv_rows()]


def _run_install_dry_run(dest: Path, model: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--dry-run",
            "--source-dir",
            str(REPO_ROOT),
            "--model",
            model,
            "--dir",
            str(dest),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw_value = line.partition("=")
        value = raw_value
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        values[key] = value
    return values


@pytest.mark.parametrize("slug", _all_slugs())
def test_dry_run_renders_correct_schema_and_env_for_every_model(tmp_path, slug):
    dest = tmp_path / f"install-{slug}"
    result = _run_install_dry_run(dest, slug)
    assert result.returncode == 0, result.stderr
    assert "no docker invocations made" in result.stdout

    registry_row = next(r for r in _committed_tsv_rows() if r["slug"] == slug)
    dim = registry_row["dim"]
    model = registry_row["model"]

    schema_text = (dest / "db" / "init" / "01_schema.sql").read_text(encoding="utf-8")
    assert f"vector({dim})" in schema_text
    assert "__EMBEDDING_DIM__" not in schema_text

    env_path = dest / ".env"
    env = _parse_env_file(env_path)
    assert env["EMBEDDING_DIM"] == dim
    assert env["EMBEDDING_MODEL_NAME"] == model
    assert env["SELF_DOCS_IMAGE_TAG"] == slug

    mode = stat.S_IMODE(env_path.stat().st_mode)
    assert mode == 0o600, f"{env_path} has mode {oct(mode)}, expected 0o600"

    security = _load_security()
    sync_token = env["SYNC_TOKEN"]
    mcp_token = env["MCP_TOKEN"]
    assert not security.is_placeholder_token(sync_token), (
        "generated SYNC_TOKEN was rejected as a placeholder by is_placeholder_token — "
        "this would trip the boot-time refusal on any non-loopback listener"
    )
    assert not security.is_placeholder_token(mcp_token), (
        "generated MCP_TOKEN was rejected as a placeholder by is_placeholder_token"
    )
    assert sync_token != mcp_token


def test_dry_run_makes_zero_docker_invocations(tmp_path):
    """Proves --dry-run truly needs no Docker: PATH is stripped of every
    real `docker` binary and replaced with a stub that exits non-zero if
    ever invoked. If the dry run still succeeds, install.sh never called it."""
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    docker_stub = fake_bin / "docker"
    docker_stub.write_text("#!/usr/bin/env bash\necho 'docker should not be called in --dry-run' >&2\nexit 99\n")
    docker_stub.chmod(0o755)

    # Keep bash/sed/curl/mkdir etc. reachable, just shadow `docker` ahead of
    # the real PATH.
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    dest = tmp_path / "no-docker-install"
    result = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--dry-run",
            "--source-dir",
            str(REPO_ROOT),
            "--dir",
            str(dest),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "docker should not be called" not in result.stderr


# ---------------------------------------------------------------------------
# 7. Re-run without --force: exit 2, .env untouched.
# ---------------------------------------------------------------------------


def test_rerun_without_force_exits_2_and_leaves_env_byte_identical(tmp_path):
    dest = tmp_path / "populated"
    first = _run_install_dry_run(dest, REGISTRY["default"])
    assert first.returncode == 0, first.stderr

    env_before = (dest / ".env").read_bytes()

    second = _run_install_dry_run(dest, REGISTRY["default"])
    assert second.returncode == 2
    assert "--force" in second.stderr

    env_after = (dest / ".env").read_bytes()
    assert env_after == env_before


def test_rerun_with_force_succeeds_and_regenerates_env(tmp_path):
    dest = tmp_path / "populated-force"
    first = _run_install_dry_run(dest, REGISTRY["default"])
    assert first.returncode == 0, first.stderr
    env_before = (dest / ".env").read_bytes()

    second = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--dry-run",
            "--force",
            "--source-dir",
            str(REPO_ROOT),
            "--model",
            REGISTRY["default"],
            "--dir",
            str(dest),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert second.returncode == 0, second.stderr
    env_after = (dest / ".env").read_bytes()
    # A fresh render regenerates secrets, so the byte content differs even
    # though the selected model is identical.
    assert env_after != env_before


# ---------------------------------------------------------------------------
# 8. deploy/docker-compose.yml shape.
# ---------------------------------------------------------------------------


def _load_compose() -> dict:
    with COMPOSE_FILE.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_compose_file_parses_as_yaml():
    compose = _load_compose()
    assert "services" in compose
    assert set(compose["services"]) == {"db", "ingestion", "mcp-server"}


def test_compose_file_has_no_build_or_profile_keys():
    compose = _load_compose()
    for name, service in compose["services"].items():
        assert "build" not in service, f"service {name!r} has a build: directive"
        assert "profiles" not in service, f"service {name!r} has a profiles: key"


def test_compose_file_publishes_both_ports_on_loopback_only():
    compose = _load_compose()
    for name in ("ingestion", "mcp-server"):
        ports = compose["services"][name]["ports"]
        assert len(ports) == 1
        assert ports[0].startswith("127.0.0.1:"), f"{name}'s port mapping {ports[0]!r} is not loopback-bound"


def test_compose_file_references_exactly_the_16_contract_variables():
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    found = set(re.findall(r"\$\{([A-Z0-9_]+)(?::-[^}]*)?\}", text))
    assert found == CONTRACT_VARS


# ---------------------------------------------------------------------------
# 9. Opt-in live-registry test: only runs with SELF_DOCS_LIVE_REGISTRY=1 (and
# a working docker buildx), so the suite stays green offline / without Docker.
# ---------------------------------------------------------------------------

_LIVE_REGISTRY_ENABLED = os.environ.get("SELF_DOCS_LIVE_REGISTRY") == "1"
_DOCKER_AVAILABLE = shutil.which("docker") is not None


def _buildx_available() -> bool:
    if not _DOCKER_AVAILABLE:
        return False
    try:
        result = subprocess.run(
            ["docker", "buildx", "version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0
    except OSError:
        return False


@pytest.mark.skipif(
    not _LIVE_REGISTRY_ENABLED,
    reason="opt-in: set SELF_DOCS_LIVE_REGISTRY=1 to check published GHCR images",
)
@pytest.mark.skipif(not _DOCKER_AVAILABLE, reason="docker CLI not available")
@pytest.mark.skipif(not _buildx_available(), reason="docker buildx not available")
@pytest.mark.parametrize("row", _committed_tsv_rows(), ids=lambda r: r["slug"])
def test_published_image_has_both_platforms_and_matching_labels(row):
    ref = f"ghcr.io/adamrussak/self-docs-ingestion:{row['slug']}"

    raw = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", ref, "--raw"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert raw.returncode == 0, raw.stderr
    manifest = json.loads(raw.stdout)
    platforms = {
        f"{m['platform']['os']}/{m['platform']['architecture']}"
        for m in manifest.get("manifests", [])
        if m.get("platform", {}).get("os") not in (None, "unknown")
    }
    assert "linux/amd64" in platforms, f"{ref} is missing a linux/amd64 manifest: {platforms}"
    assert "linux/arm64" in platforms, f"{ref} is missing a linux/arm64 manifest: {platforms}"

    detail = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", ref, "--format", "{{json .}}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert detail.returncode == 0, detail.stderr
    parsed = json.loads(detail.stdout)
    labels = parsed["image"]["linux/amd64"]["config"]["Labels"]
    assert labels["io.self-docs.embedding-model"] == row["model"]
    assert labels["io.self-docs.embedding-dim"] == row["dim"]


# ---------------------------------------------------------------------------
# 10. Manifest field validation (security-review fix contract).
#
# Contract: every field parsed out of models.tsv is validated immediately
# after parsing, before any use — dim must match ^[0-9]+$, slug must match
# ^[a-z0-9][a-z0-9._-]*$, and prompt fields must not contain '"' or control
# characters. A violation exits 2 naming the offending row/field. These tests
# build a SYNTHETIC models.tsv under a temp --source-dir; the committed
# deploy/models.tsv is never edited.
# ---------------------------------------------------------------------------


def _build_synthetic_checkout(tmp_path: Path, tsv_content: str) -> Path:
    """A minimal fake self-docs checkout for --source-dir: just enough of
    deploy/ and db/init/ for install.sh to run against a synthetic, single-row
    deploy/models.tsv (is_default=true, so no --model flag is needed)."""
    checkout = tmp_path / f"synthetic-checkout-{uuid.uuid4().hex[:8]}"
    (checkout / "deploy").mkdir(parents=True)
    (checkout / "db" / "init").mkdir(parents=True)
    (checkout / "deploy" / "models.tsv").write_text(tsv_content, encoding="utf-8")
    shutil.copy(COMPOSE_FILE, checkout / "deploy" / "docker-compose.yml")
    shutil.copy(
        REPO_ROOT / "db" / "init" / "01_schema.sql.template",
        checkout / "db" / "init" / "01_schema.sql.template",
    )
    for name in ("02_sources_config.sql", "03_fix_embedding_dim.sql", "04_upload_sources.sql"):
        (checkout / "db" / "init" / name).write_text("-- stub\n", encoding="utf-8")
    return checkout


def _run_install_synthetic(dest: Path, source_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALL_SH), "--dry-run", "--source-dir", str(source_dir), "--dir", str(dest)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _assert_schema_absent_or_free_of(dest: Path, forbidden: list[str]) -> None:
    """The rendered-schema assertion the security reviewer's reproduction
    hinged on: if a schema file exists at all after a rejected install, it
    must never contain the offending value baked into a `vector(...)` column
    (or any other injected fragment)."""
    schema_path = dest / "db" / "init" / "01_schema.sql"
    if not schema_path.exists():
        return
    text = schema_path.read_text(encoding="utf-8", errors="replace")
    for bad in forbidden:
        assert bad not in text, f"{schema_path} contains forbidden pattern {bad!r}: {text!r}"


def test_non_numeric_dim_is_rejected_and_never_rendered_into_schema(tmp_path):
    """EXPECTED RED pre-fix: verified by hand against this worktree's
    current, unpatched install.sh — today this exits 0 and writes
    `embedding     vector(not-a-number) NOT NULL` straight into
    db/init/01_schema.sql."""
    tsv = "test-model|vendor/test-model|not-a-number|1500m|1g|prompt: |passage: |true\n"
    checkout = _build_synthetic_checkout(tmp_path, tsv)
    dest = tmp_path / "dest-nonnumeric-dim"
    result = _run_install_synthetic(dest, checkout)
    assert result.returncode == 2, f"expected exit 2 for a non-numeric dim, got {result.returncode}: {result.stderr}"
    assert "dim" in result.stderr.lower()
    _assert_schema_absent_or_free_of(dest, ["vector(not-a-number)"])


def test_empty_dim_is_rejected_and_never_rendered_into_schema(tmp_path):
    """EXPECTED RED pre-fix: verified by hand against this worktree's
    current, unpatched install.sh — today this exits 0 and writes
    `embedding     vector() NOT NULL` straight into db/init/01_schema.sql."""
    tsv = "test-model|vendor/test-model||1500m|1g|prompt: |passage: |true\n"
    checkout = _build_synthetic_checkout(tmp_path, tsv)
    dest = tmp_path / "dest-empty-dim"
    result = _run_install_synthetic(dest, checkout)
    assert result.returncode == 2, f"expected exit 2 for an empty dim, got {result.returncode}: {result.stderr}"
    assert "dim" in result.stderr.lower()
    _assert_schema_absent_or_free_of(dest, ["vector()"])


def test_sed_metacharacter_dim_is_rejected_and_never_reaches_sed(tmp_path):
    """EXPECTED RED pre-fix: verified by hand against this worktree's
    current, unpatched install.sh — today this dim value reaches the
    unguarded `sed "s/__EMBEDDING_DIM__/${SEL_DIM}/g"` substitution verbatim.
    On GNU sed, this dim's trailing `;e id` is a second sed command using the
    `e` extension (execute a shell command) — the concrete RCE the reviewer
    flagged. On this machine's BSD sed it instead errors out ("invalid
    command code e"), which is still not a clean, validated exit-2 usage
    error (it's an uncaught crash under `set -e`), so this test is red on
    both sed dialects for the same underlying reason: nothing validates the
    field before it reaches sed at all."""
    tsv = "test-model|vendor/test-model|384/g;e id|1500m|1g|prompt: |passage: |true\n"
    checkout = _build_synthetic_checkout(tmp_path, tsv)
    dest = tmp_path / "dest-sed-metachar-dim"
    result = _run_install_synthetic(dest, checkout)
    assert result.returncode == 2, (
        f"expected exit 2 for a dim containing sed metacharacters, got {result.returncode}: {result.stderr}"
    )
    _assert_schema_absent_or_free_of(dest, ["384/g", "vector(384/g"])


def test_hostile_slug_is_rejected_and_no_env_is_written(tmp_path):
    """EXPECTED RED pre-fix: verified by hand against this worktree's
    current, unpatched install.sh — today this exits 0 and writes
    `SELF_DOCS_IMAGE_TAG=evil:tag@sha256` straight into .env. A colon/@ in an
    image tag position is exactly the shape Docker/GHCR parse as a
    registry-or-digest separator, i.e. an attacker-steerable image
    reference."""
    tsv = "evil:tag@sha256|vendor/test-model|384|1500m|1g|prompt: |passage: |true\n"
    checkout = _build_synthetic_checkout(tmp_path, tsv)
    dest = tmp_path / "dest-hostile-slug"
    result = _run_install_synthetic(dest, checkout)
    assert result.returncode == 2, f"expected exit 2 for a hostile slug, got {result.returncode}: {result.stderr}"
    assert "slug" in result.stderr.lower()
    assert not (dest / ".env").exists()


def test_prompt_containing_double_quote_is_rejected_and_no_env_is_written(tmp_path):
    """EXPECTED RED pre-fix: verified by hand against this worktree's
    current, unpatched install.sh — today this exits 0 and writes
    `EMBEDDING_QUERY_PROMPT="foo"bar"` into .env: an odd number of quote
    characters on that line, the malformed-quoting shape the reviewer
    reported as corrupting later lines of the file."""
    tsv = 'test-model|vendor/test-model|384|1500m|1g|foo"bar|passage: |true\n'
    checkout = _build_synthetic_checkout(tmp_path, tsv)
    dest = tmp_path / "dest-hostile-prompt"
    result = _run_install_synthetic(dest, checkout)
    assert result.returncode == 2, (
        f"expected exit 2 for a prompt containing '\"', got {result.returncode}: {result.stderr}"
    )
    assert not (dest / ".env").exists()


def test_negative_control_real_committed_tsv_still_validates_and_all_models_resolve(tmp_path):
    """Guards against the fix over-rejecting legitimate rows — its own kind
    of outage. Every one of the real, committed deploy/models.tsv's rows must
    still install cleanly under --dry-run once field validation lands.
    Already implicitly exercised by section 6's per-model dry-run tests; this
    one exists as an explicit, minimal negative control for this fix."""
    for row in _committed_tsv_rows():
        dest = tmp_path / f"negative-control-{row['slug']}"
        result = _run_install_dry_run(dest, row["slug"])
        assert result.returncode == 0, (
            f"model {row['slug']!r} from the real committed manifest was rejected: {result.stderr}"
        )
        assert (dest / ".env").exists()


# ---------------------------------------------------------------------------
# 11. Option-value injection guard: an option value beginning with '-' must
# be rejected with a clean usage error, not silently consumed as though it
# were the next flag (or, worse, later fed to a system utility that treats a
# leading '-' as ITS OWN option).
# ---------------------------------------------------------------------------


def test_dir_value_starting_with_dash_is_rejected_with_usage_error(tmp_path):
    """EXPECTED RED pre-fix: verified by hand against this worktree's
    current, unpatched install.sh — today DIR is silently set to the literal
    string "--force" (parsing already consumed both tokens as --dir's value,
    so --force mode itself is never entered), and BSD `dirname` later chokes
    on that value ("illegal option -- -"), producing an uncontrolled crash
    (exit 1) instead of a clean, validated exit-2 usage error."""
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--dry-run", "--source-dir", str(REPO_ROOT), "--dir", "--force"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )
    assert result.returncode == 2, (
        f"expected exit 2 usage error for --dir taking a value starting with '-', "
        f"got {result.returncode}: {result.stderr}"
    )
    assert not (tmp_path / "--force").exists()


# ---------------------------------------------------------------------------
# 12. awk-injection regression (check_disk_space's disk-space warning).
#
# check_disk_space() splices `${parent}` (derived by climbing --dir's
# ancestors until one exists) UNQUOTED into a single-quoted awk BEGIN{}
# program string, unlike avail_kb/pair_gb/slug, which are safely passed via
# `-v`. A --dir value crafted to break out of that quoting reaches an
# attacker-controlled `awk ... BEGIN { system(...) }` block.
# ---------------------------------------------------------------------------


def test_install_sh_does_not_execute_awk_injection_from_hostile_dir_name(tmp_path):
    """Regression guard for the awk-injection finding: a --dir name crafted
    to break out of check_disk_space's unquoted awk BEGIN{} splice must never
    execute. Uses the security reviewer's exact reproduction directory name
    verbatim (still a valid POSIX path component — this worktree's
    filesystem handles it fine, so no simplified stand-in name was needed). A
    unique per-test marker stands in for the reviewer's `id -u`, so a
    leftover marker from a previous run can never produce a false pass.

    NOT a plain substring check: install.sh legitimately echoes the path it
    just wrote to (e.g. "install.sh: wrote <DIR>/docker-compose.yml"), and
    the payload marker is embedded IN that directory name — so the marker is
    unavoidably a substring of correct, non-exploited output too. What
    distinguishes "executed" from "path printed" is that an executed
    `echo <marker> >&2` (from awk's `system()`) emits a line whose entire
    stripped content IS the marker; a path echo only ever contains the
    marker as part of a longer line. So this asserts no output LINE, in
    full, equals the marker — checking stdout and stderr, since the payload
    targets stderr (`>&2`) but nothing here requires that split to hold.

    EXPECTED RED pre-fix: verified by hand against this worktree's current,
    unpatched install.sh — a standalone line consisting solely of the marker
    DOES appear on stderr (arbitrary shell execution via awk's `system()`),
    and the run still exits 0."""
    marker = f"RCE-MARKER-{uuid.uuid4().hex}"
    payload_dir_name = 'x", 1,2,3 } } BEGIN { system("echo ' + marker + ' >&2") } BEGIN { if (1) { print "y'
    dest = tmp_path / payload_dir_name
    dest.mkdir()

    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--dry-run", "--source-dir", str(REPO_ROOT), "--dir", str(dest)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    executed = [line for line in (result.stdout + result.stderr).splitlines() if line.strip() == marker]
    assert not executed, f"awk injection executed: marker appeared as standalone output line(s): {executed}"


# ---------------------------------------------------------------------------
# 13. db/init/*.sql file-mode symmetry (final-review fix).
#
# Contract: after rendering, ALL FOUR db/init/*.sql files (01_schema.sql,
# rendered via awk/shell redirection, and 02-04, fetched via
# mktemp(0600)->mv->chmod) must be mode 0644 so postgres — a non-root,
# non-installing-user uid on a Linux bind mount — can read every one of
# them; only .env stays 0600. Asserted on all four explicitly, per the task
# dispatch notes: the whole bug was that exactly one of the four differed
# from the other three, so sampling a single file would have missed it.
# ---------------------------------------------------------------------------


def test_all_four_sql_files_are_mode_0644_and_env_stays_mode_0600(tmp_path):
    """EXPECTED RED pre-fix: verified by hand against this worktree's
    current, unpatched install.sh — 01_schema.sql (written by `sed ... >
    file` under the ambient umask) lands at 0644, but 02_sources_config.sql,
    03_fix_embedding_dim.sql and 04_upload_sources.sql (fetched via
    mktemp(0600)->mv, which preserves mktemp's 0600) all land at 0600 —
    unreadable by postgres's uid on a real Linux bind mount."""
    dest = tmp_path / "sql-mode-check"
    result = _run_install_dry_run(dest, REGISTRY["default"])
    assert result.returncode == 0, result.stderr

    for name in ("01_schema.sql", "02_sources_config.sql", "03_fix_embedding_dim.sql", "04_upload_sources.sql"):
        sql_path = dest / "db" / "init" / name
        mode = stat.S_IMODE(sql_path.stat().st_mode)
        assert mode == 0o644, f"{sql_path} has mode {oct(mode)}, expected 0o644"

    env_mode = stat.S_IMODE((dest / ".env").stat().st_mode)
    assert env_mode == 0o600, f"{dest / '.env'} has mode {oct(env_mode)}, expected 0o600"


# ---------------------------------------------------------------------------
# 14. --force must not destroy a surviving pgdata volume's password
# (final-review fix).
#
# Contract: POSTGRES_PASSWORD is preserved across --force whenever the
# `pgdata` volume's existence can't be conclusively ruled out (no docker, no
# docker-compose.yml yet, `docker compose config`/`docker volume inspect`
# unavailable or inconclusive — including the ENTIRETY of --dry-run, which
# makes zero docker calls) and only regenerated when `docker volume inspect`
# CONFIRMS the volume no longer exists. SYNC_TOKEN/MCP_TOKEN carry no such
# protection — read from the actual script (see module docstring for the
# diff), they are unconditionally regenerated on every render, --force or
# not, dry-run or not. That asymmetry is real, not a test author's
# assumption, and is pinned explicitly below so a later refactor can't
# quietly flatten it (e.g. by "simplifying" all three secrets to the same
# code path).
# ---------------------------------------------------------------------------


def _read_env_secrets(env_path: Path) -> dict[str, str]:
    env = _parse_env_file(env_path)
    return {k: env[k] for k in ("POSTGRES_PASSWORD", "SYNC_TOKEN", "MCP_TOKEN")}


def test_dry_run_force_preserves_postgres_password_but_regenerates_tokens(tmp_path):
    """The safe-direction branch, testable with zero Docker: in --dry-run,
    volume detection is skipped entirely (VOL_STATUS stays "inconclusive"),
    which this fix treats the same as "could not confirm absence" ->
    preserve. EXPECTED RED pre-fix: verified by hand against this worktree's
    current, unpatched install.sh — POSTGRES_PASSWORD is unconditionally
    regenerated on every render, so a --force re-run silently changes it even
    though nothing here could possibly have confirmed the old pgdata volume
    (if any) is gone."""
    dest = tmp_path / "force-preserve-dry-run"
    first = _run_install_dry_run(dest, REGISTRY["default"])
    assert first.returncode == 0, first.stderr
    before = _read_env_secrets(dest / ".env")

    second = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--dry-run",
            "--force",
            "--source-dir",
            str(REPO_ROOT),
            "--model",
            REGISTRY["default"],
            "--dir",
            str(dest),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert second.returncode == 0, second.stderr
    after = _read_env_secrets(dest / ".env")

    assert after["POSTGRES_PASSWORD"] == before["POSTGRES_PASSWORD"], (
        "--force with inconclusive volume detection must PRESERVE POSTGRES_PASSWORD "
        "(a fresh one would lock the operator out of a surviving pgdata volume)"
    )
    # Pin the asymmetry: unlike POSTGRES_PASSWORD, these are regenerated
    # unconditionally on every render.
    assert after["SYNC_TOKEN"] != before["SYNC_TOKEN"], (
        "SYNC_TOKEN is expected to be unconditionally regenerated on every render, "
        "including this preserve-password path — if this now fails, the asymmetry "
        "documented in this test's docstring has changed and should be re-verified, "
        "not silently accepted"
    )
    assert after["MCP_TOKEN"] != before["MCP_TOKEN"], (
        "MCP_TOKEN is expected to be unconditionally regenerated on every render, "
        "including this preserve-password path — if this now fails, the asymmetry "
        "documented in this test's docstring has changed and should be re-verified, "
        "not silently accepted"
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_fake_docker_for_volume_detection(bin_dir: Path) -> None:
    """A minimal `docker` stub, placed ahead of the real one on PATH, that
    answers just enough of the real CLI surface for a non-dry-run install to
    reach `detect_pgdata_volume_status` and stop cleanly (via --no-start
    --no-verify-labels) without ever touching a real daemon:
      - `compose version`      -> a v2-shaped version string
      - `info`                 -> ok (daemon "reachable")
      - `compose config ...`   -> a fixed fake Compose project name as JSON
      - `compose pull`         -> ok (nothing to actually pull)
      - `compose up ...`       -> ok instantly (only reached when a test
                                   deliberately omits --no-start, to prove the
                                   success banner is genuinely absent/present
                                   rather than merely skipped by --no-start)
      - `volume inspect ...`   -> exit 0/1 per $FAKE_DOCKER_VOLUME_EXISTS,
                                   i.e. the one thing this test controls
    Anything else is an unexpected call this stub doesn't understand -> fails
    loudly (exit 1, message on stderr) rather than silently no-op'ing.
    """
    docker_stub = bin_dir / "docker"
    docker_stub.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if [[ "$1" == "compose" && "$2" == "version" ]]; then\n'
        '    echo "Docker Compose version v2.99.9"\n'
        "    exit 0\n"
        "fi\n"
        'if [[ "$1" == "info" ]]; then\n'
        "    exit 0\n"
        "fi\n"
        'if [[ "$1" == "compose" && "$2" == "config" ]]; then\n'
        '    echo \'{"name": "faketest"}\'\n'
        "    exit 0\n"
        "fi\n"
        'if [[ "$1" == "compose" && "$2" == "pull" ]]; then\n'
        "    exit 0\n"
        "fi\n"
        'if [[ "$1" == "compose" && "$2" == "up" ]]; then\n'
        "    exit 0\n"
        "fi\n"
        'if [[ "$1" == "volume" && "$2" == "inspect" ]]; then\n'
        '    [[ "${FAKE_DOCKER_VOLUME_EXISTS:-0}" == "1" ]] && exit 0 || exit 1\n'
        "fi\n"
        'echo "fake docker: unhandled args: $*" >&2\n'
        "exit 1\n"
    )
    docker_stub.chmod(0o755)


def _run_install_non_dry_run(
    dest: Path,
    *,
    force: bool,
    volume_exists: bool,
    env_overrides: dict[str, str],
    model: str = REGISTRY["default"],
    no_start: bool = True,
) -> subprocess.CompletedProcess[str]:
    args = [
        "bash",
        str(INSTALL_SH),
        "--source-dir",
        str(REPO_ROOT),
        "--model",
        model,
        "--dir",
        str(dest),
        "--port-api",
        str(_free_port()),
        "--port-mcp",
        str(_free_port()),
        "--no-verify-labels",
    ]
    if no_start:
        args.append("--no-start")
    if force:
        args.insert(2, "--force")
    env = os.environ.copy()
    env.update(env_overrides)
    env["FAKE_DOCKER_VOLUME_EXISTS"] = "1" if volume_exists else "0"
    return subprocess.run(args, capture_output=True, text=True, timeout=60, env=env)


def test_force_with_confirmed_absent_volume_generates_a_fresh_password(tmp_path):
    """The confirmed-ABSENT branch (VOL_STATUS=1): a fake `docker` on PATH
    answers `docker volume inspect <project>_pgdata` with "not found", which
    is the one case where regenerating POSTGRES_PASSWORD on --force is
    actually safe (there is no surviving initialized volume to lock anyone
    out of). EXPECTED RED pre-fix in a DIFFERENT way than the other tests in
    this section: this assertion (password DOES change) already holds today,
    since today's install.sh always regenerates unconditionally — this test
    exists to pin the CORRECT reason once the fix lands (a confirmed-absent
    volume), not merely a coincidentally-matching pre-fix behavior."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_fake_docker_for_volume_detection(bin_dir)
    path_env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    dest = tmp_path / "force-vol-absent"
    first = _run_install_non_dry_run(dest, force=False, volume_exists=False, env_overrides=path_env)
    assert first.returncode == 0, first.stderr
    before = _parse_env_file(dest / ".env")["POSTGRES_PASSWORD"]

    second = _run_install_non_dry_run(dest, force=True, volume_exists=False, env_overrides=path_env)
    assert second.returncode == 0, second.stderr
    after = _parse_env_file(dest / ".env")["POSTGRES_PASSWORD"]

    assert after != before, (
        "--force with a CONFIRMED-ABSENT pgdata volume should generate a fresh "
        "POSTGRES_PASSWORD (no surviving volume to lock anyone out of)"
    )


def test_force_with_confirmed_present_volume_preserves_the_password(tmp_path):
    """The confirmed-PRESENT branch (VOL_STATUS=0): a fake `docker` on PATH
    answers `docker volume inspect <project>_pgdata` with "found", which is
    the exact scenario the whole fix targets — a real, surviving pgdata
    volume that only ever accepted the OLD password. EXPECTED RED pre-fix:
    verified by hand against this worktree's current, unpatched install.sh —
    it regenerates POSTGRES_PASSWORD unconditionally here too, which is
    precisely the destructive case the fix addresses."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_fake_docker_for_volume_detection(bin_dir)
    path_env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    dest = tmp_path / "force-vol-present"
    first = _run_install_non_dry_run(dest, force=False, volume_exists=True, env_overrides=path_env)
    assert first.returncode == 0, first.stderr
    before = _parse_env_file(dest / ".env")["POSTGRES_PASSWORD"]

    second = _run_install_non_dry_run(dest, force=True, volume_exists=True, env_overrides=path_env)
    assert second.returncode == 0, second.stderr
    after = _parse_env_file(dest / ".env")["POSTGRES_PASSWORD"]

    assert after == before, (
        "--force with a CONFIRMED-PRESENT pgdata volume must preserve POSTGRES_PASSWORD "
        "(postgres only applies it once, to an empty volume — a new one here would "
        "lock the operator out of their own already-indexed corpus)"
    )


# ---------------------------------------------------------------------------
# 15. --force must refuse a MODEL change over a confirmed-present pgdata
# volume (regression introduced by section 14's own fix).
#
# Preserving POSTGRES_PASSWORD over a surviving volume is right, but nothing
# previously checked that the newly selected model still matches what that
# volume was initialized for. Postgres never re-applies db/init to a
# non-empty volume, so: install (bge-small, 384) -> `docker compose down` ->
# `--force --model bge-base-en-v1.5` over the SAME (confirmed-present)
# volume used to exit 0 and print "self-docs is up." with the image/schema
# now at dim 768 while the live column stayed vector(384) — the exact
# dimension-mismatch failure this whole kit exists to prevent. It was
# previously masked by an accident: the regenerated password broke auth
# before the mismatch could surface; fixing section 14's bug removed that
# accidental cover.
#
# Contract: in the volume-confirmed-present branch, the installer reads
# EMBEDDING_MODEL_NAME/EMBEDDING_DIM from the existing .env and refuses (exit
# 2, die_usage) when the newly selected model differs, naming both models,
# both dimensions, and `docker compose down -v` as the deliberate destructive
# path. These tests deliberately omit --no-start (unlike section 14's
# tests): the point of asserting the success banner's ABSENCE only means
# something if the run could otherwise have reached it, so the shared fake
# `docker` stub above also answers `compose up` now.
# ---------------------------------------------------------------------------


def test_force_with_different_model_over_confirmed_present_volume_is_refused(tmp_path):
    """EXPECTED RED pre-fix: verified by hand against the current `wt/INT`
    mirror (which already has section 14's --force password-preserve fix,
    but NOT this model-match check yet) — the second run below exits 0 and
    prints "self-docs is up." despite installing bge-base-en-v1.5 (dim 768)
    over a volume confirmed to still hold bge-small-en-v1.5 (dim 384) data.
    Asserts on exit code and the banner's absence first; the model/dim-naming
    checks below are supplementary, scoped to stderr only (die_usage's own
    output channel) specifically to avoid a stdout admin-URL line's port
    number ever coincidentally containing a `dim` substring."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_fake_docker_for_volume_detection(bin_dir)
    path_env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    dest = tmp_path / "force-model-mismatch"
    first = _run_install_non_dry_run(
        dest,
        force=False,
        volume_exists=True,
        env_overrides=path_env,
        model="bge-small-en-v1.5",
        no_start=False,
    )
    assert first.returncode == 0, first.stderr

    second = _run_install_non_dry_run(
        dest,
        force=True,
        volume_exists=True,
        env_overrides=path_env,
        model="bge-base-en-v1.5",
        no_start=False,
    )
    assert second.returncode != 0, (
        f"expected a refusal (non-zero exit) for --force with a DIFFERENT model over "
        f"a confirmed-present pgdata volume, got exit {second.returncode}: {second.stderr}"
    )
    assert "self-docs is up" not in second.stdout, (
        "installer printed the success banner despite a model/dimension mismatch "
        "against a surviving pgdata volume — this is the exact bug being pinned"
    )
    assert "384" in second.stderr and "768" in second.stderr, (
        f"expected the refusal message to name both dimensions (384, 768): {second.stderr!r}"
    )
    assert "bge-small" in second.stderr and "bge-base" in second.stderr, (
        f"expected the refusal message to name both models: {second.stderr!r}"
    )
    assert "down -v" in second.stderr, (
        f"expected the refusal message to name 'docker compose down -v' as the deliberate "
        f"destructive path: {second.stderr!r}"
    )


def test_force_with_same_model_over_confirmed_present_volume_still_succeeds(tmp_path):
    """The negative control this fix needs as much as the refusal itself:
    over-rejecting --force when the model DID NOT change would silently undo
    section 14's own password-preservation fix. ALREADY PASSES before this
    round's fix lands — explicitly noted, not silently relied on: nothing
    before this round rejects a same-model --force, and section 14 already
    proved a confirmed-present volume preserves the password in this exact
    shape. This test exists so that if a future model-match implementation
    over-broadens its refusal condition (e.g. comparing dims as strings vs
    ints, or refusing whenever ANY --model flag is passed at all), it fails
    here immediately — not to prove the new refusal exists."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_fake_docker_for_volume_detection(bin_dir)
    path_env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    dest = tmp_path / "force-model-match"
    first = _run_install_non_dry_run(
        dest,
        force=False,
        volume_exists=True,
        env_overrides=path_env,
        model=REGISTRY["default"],
        no_start=False,
    )
    assert first.returncode == 0, first.stderr
    before = _parse_env_file(dest / ".env")["POSTGRES_PASSWORD"]

    second = _run_install_non_dry_run(
        dest,
        force=True,
        volume_exists=True,
        env_overrides=path_env,
        model=REGISTRY["default"],
        no_start=False,
    )
    assert second.returncode == 0, second.stderr
    assert "self-docs is up" in second.stdout, (
        f"a --force re-run with the SAME model over a confirmed-present volume must still succeed: {second.stderr!r}"
    )
    after = _parse_env_file(dest / ".env")["POSTGRES_PASSWORD"]
    assert after == before


def test_force_with_confirmed_present_volume_and_missing_old_password_refuses(tmp_path):
    """A related edge case flagged alongside the model-match fix, not
    confirmed to land in the same commit (the dispatch notes call it "a
    parallel should-fix"): if `--force` targets a confirmed-present volume
    but the existing .env has no POSTGRES_PASSWORD= line at all (deleted,
    corrupted, hand-edited), there is nothing to preserve, and today's
    install.sh (even with section 14's fix) silently falls through to
    generating a FRESH password — recreating section 14's exact bug via a
    different trigger. EXPECTED RED against both this worktree and the
    current wt/INT mirror; UNCONFIRMED whether it flips green at the same
    time as the two tests above, since wt/INT does not yet contain ANY
    model-match/refusal logic to check that against — included here anyway
    per the dispatch notes ("if it's cheap with your stub, add it"), since
    building this scenario needed no new stub capability, only deleting a
    line from an already-rendered .env."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_fake_docker_for_volume_detection(bin_dir)
    path_env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    dest = tmp_path / "force-missing-password"
    first = _run_install_non_dry_run(
        dest,
        force=False,
        volume_exists=True,
        env_overrides=path_env,
        model=REGISTRY["default"],
        no_start=False,
    )
    assert first.returncode == 0, first.stderr

    env_path = dest / ".env"
    stripped = "\n".join(
        line for line in env_path.read_text(encoding="utf-8").splitlines() if not line.startswith("POSTGRES_PASSWORD=")
    )
    env_path.write_text(stripped + "\n", encoding="utf-8")
    env_path.chmod(0o600)

    second = _run_install_non_dry_run(
        dest,
        force=True,
        volume_exists=True,
        env_overrides=path_env,
        model=REGISTRY["default"],
        no_start=False,
    )
    assert second.returncode != 0, (
        f"expected a refusal (non-zero exit) for --force over a confirmed-present volume "
        f"with no POSTGRES_PASSWORD to preserve, got exit {second.returncode}: {second.stderr}"
    )
    assert "self-docs is up" not in second.stdout, (
        "installer printed the success banner after silently generating a fresh password "
        "against a surviving pgdata volume it could not confirm the old password for"
    )
    assert "down -v" in second.stderr, (
        f"expected the refusal message to name 'docker compose down -v': {second.stderr!r}"
    )


# ---------------------------------------------------------------------------
# 16. A REFUSED --force must leave db/init/01_schema.sql on disk untouched
# (follow-up to section 15's own fix).
#
# Section 15 added the refusal, but schema rendering happened BEFORE that
# check ran — so a refused --force still overwrote 01_schema.sql for the
# rejected model while .env correctly kept the old one. Not inert: the
# refusal message's own recommended recovery (`docker compose down -v` then
# `docker compose up -d`, also the runbook's documented normal operation)
# applies db/init fresh to the now-empty volume, baking the REJECTED
# dimension in for real, while the running image is still labeled for the
# OLD model — an install/query-time dimension mismatch, silently caused by
# following the tool's own advice.
#
# Contract: the 01_schema.sql render moves below the volume/model check. All
# three tests reuse section 14/15's fake-`docker`-on-PATH stub with the
# volume reported present.
# ---------------------------------------------------------------------------


def test_refused_force_model_change_leaves_on_disk_schema_byte_identical(tmp_path):
    """The regression test. EXPECTED RED against both this worktree and the
    current wt/INT mirror: verified by hand — a refused --force (bge-small
    384 -> bge-base 768, volume confirmed present) exits 2 with the correct
    refusal message, but db/init/01_schema.sql on disk has ALREADY been
    rewritten to vector(768) by the time that refusal fires. Snapshots the
    file's raw bytes before the refused run and asserts byte-for-byte
    equality after — stronger than a substring/vector(N) check and immune to
    the false-positive class hit twice already in this file (a marker/value
    that is also legitimately present elsewhere in correct output)."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_fake_docker_for_volume_detection(bin_dir)
    path_env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    dest = tmp_path / "schema-untouched-on-refusal"
    first = _run_install_non_dry_run(
        dest,
        force=False,
        volume_exists=True,
        env_overrides=path_env,
        model="bge-small-en-v1.5",
        no_start=False,
    )
    assert first.returncode == 0, first.stderr
    schema_path = dest / "db" / "init" / "01_schema.sql"
    schema_before = schema_path.read_bytes()

    second = _run_install_non_dry_run(
        dest,
        force=True,
        volume_exists=True,
        env_overrides=path_env,
        model="bge-base-en-v1.5",
        no_start=False,
    )
    assert second.returncode != 0, (
        f"expected the model-change refusal (section 15) to still fire here, got exit "
        f"{second.returncode}: {second.stderr}"
    )
    assert "self-docs is up" not in second.stdout, (
        "installer printed the success banner despite refusing the model change"
    )

    schema_after = schema_path.read_bytes()
    assert schema_after == schema_before, (
        "a REFUSED --force must leave db/init/01_schema.sql byte-for-byte untouched — "
        "if this fails, the schema render still runs (and overwrites the file for the "
        "REJECTED model) before the model-match check, exactly the bug this test pins"
    )


@pytest.mark.parametrize("row", _committed_tsv_rows(), ids=lambda r: r["slug"])
def test_force_same_model_over_confirmed_present_volume_still_renders_schema(tmp_path, row):
    """The happy-path control this fix needs as much as the regression test:
    moving a render is exactly the kind of change that can silently skip it
    on some other path. Covers a fresh install AND a same-model --force
    (both go through the exact code path section 16 touches — the volume/
    model check plus the now-relocated render — unlike section 6's existing
    per-model --dry-run tests, which never reach the volume-detection branch
    at all) for all four registry models.

    ALREADY PASSES before this round's fix lands, for a real reason, not
    coincidence: for the SAME model, section 15's check
    (`OLD_EMBEDDING_MODEL_NAME != SEL_MODEL || OLD_EMBEDDING_DIM != SEL_DIM`)
    is false either way, so nothing refuses and the dimension never changes
    regardless of whether the render happens before or after that check.
    Moving the render only changes behavior on the REFUSAL path (section
    16's actual regression test above), never on this one. Kept as the
    required control: a future change to the render's relocation that
    accidentally guards it on the wrong branch (e.g. skipping it whenever
    --force is set at all, not just when refused) would fail here."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_fake_docker_for_volume_detection(bin_dir)
    path_env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    dest = tmp_path / f"schema-happy-{row['slug']}"
    first = _run_install_non_dry_run(
        dest,
        force=False,
        volume_exists=True,
        env_overrides=path_env,
        model=row["slug"],
        no_start=False,
    )
    assert first.returncode == 0, first.stderr
    schema_path = dest / "db" / "init" / "01_schema.sql"
    text = schema_path.read_text(encoding="utf-8")
    assert f"vector({row['dim']})" in text
    assert "__EMBEDDING_DIM__" not in text

    second = _run_install_non_dry_run(
        dest,
        force=True,
        volume_exists=True,
        env_overrides=path_env,
        model=row["slug"],
        no_start=False,
    )
    assert second.returncode == 0, second.stderr
    assert "self-docs is up" in second.stdout, (
        f"a --force re-run with the SAME model over a confirmed-present volume must still succeed: {second.stderr!r}"
    )
    text2 = schema_path.read_text(encoding="utf-8")
    assert f"vector({row['dim']})" in text2
    assert "__EMBEDDING_DIM__" not in text2


def test_force_with_present_volume_and_env_missing_model_name_now_refuses(tmp_path):
    """A related edge case bundled with this round's fix, per the dispatch
    notes: POSTGRES_PASSWORD present in the old .env (so the password-
    recovery check alone wouldn't refuse) but EMBEDDING_MODEL_NAME absent
    (hand-edited/corrupted .env) — there is nothing to compare the newly
    selected model against, so this must refuse rather than silently
    skipping the model-match check and proceeding. Cheap to add: needed no
    new stub capability, only deleting one line from an already-rendered
    .env, the same technique as section 15's missing-password test.

    EXPECTED RED against both this worktree and the current wt/INT mirror:
    verified by hand — today, an empty OLD_EMBEDDING_MODEL_NAME short-
    circuits section 15's `[[ -n "$OLD_EMBEDDING_MODEL_NAME" ]] && ...` check
    entirely, so it neither refuses nor validates anything; the install
    proceeds to exit 0 and prints the success banner."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_fake_docker_for_volume_detection(bin_dir)
    path_env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    dest = tmp_path / "force-missing-model-name"
    first = _run_install_non_dry_run(
        dest,
        force=False,
        volume_exists=True,
        env_overrides=path_env,
        model=REGISTRY["default"],
        no_start=False,
    )
    assert first.returncode == 0, first.stderr

    env_path = dest / ".env"
    stripped = "\n".join(
        line
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("EMBEDDING_MODEL_NAME=")
    )
    env_path.write_text(stripped + "\n", encoding="utf-8")
    env_path.chmod(0o600)
    assert _parse_env_file(env_path).get("POSTGRES_PASSWORD"), (
        "test setup bug: POSTGRES_PASSWORD must survive the strip"
    )

    second = _run_install_non_dry_run(
        dest,
        force=True,
        volume_exists=True,
        env_overrides=path_env,
        model=REGISTRY["default"],
        no_start=False,
    )
    assert second.returncode != 0, (
        f"expected a refusal (non-zero exit) for --force over a confirmed-present volume "
        f"whose .env has no EMBEDDING_MODEL_NAME to validate against, got exit "
        f"{second.returncode}: {second.stderr}"
    )
    assert "self-docs is up" not in second.stdout, (
        "installer printed the success banner despite being unable to confirm the "
        "existing volume's model against the newly selected one"
    )
