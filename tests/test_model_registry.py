"""Guards that keep the embedding-model registry, the two services' defaults,
the rendered schema, and the duplicated SSRF helper from drifting apart.

Runs under the ingestion venv (see Makefile's `test` target), so `app` here is
the ingestion package and PyYAML is available. The mcp-server side has its own
`tests/test_registry_defaults.py` for its runtime defaults.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "config" / "models.yaml"
SCHEMA_FILE = REPO_ROOT / "db" / "init" / "01_schema.sql"
INGESTION_URLSCOPE = REPO_ROOT / "ingestion" / "app" / "urlscope.py"
MCP_RETRIEVAL = REPO_ROOT / "mcp-server" / "app" / "retrieval.py"


def _load_registry() -> dict:
    with REGISTRY_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _load_configure_model():
    spec = importlib.util.spec_from_file_location(
        "configure_model", REPO_ROOT / "scripts" / "configure_model.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REGISTRY = _load_registry()


def test_registry_default_is_a_known_model():
    assert REGISTRY["default"] in REGISTRY["models"]


@pytest.mark.parametrize("name", list(REGISTRY["models"]))
def test_registry_rows_are_complete(name):
    row = REGISTRY["models"][name]
    assert isinstance(row["dim"], int) and row["dim"] > 0
    assert row["mem_ingestion"] and row["mem_mcp"]
    # Prompts are required keys (may be empty strings) so the services and the
    # configure script can rely on them existing.
    assert "query_prompt" in row
    assert "passage_prompt" in row


def test_committed_schema_matches_rendered_default():
    """db/init/01_schema.sql must equal render(template, default-model dim). If
    this fails, run `make configure` (default model) to regenerate it."""
    configure_model = _load_configure_model()
    default_dim = REGISTRY["models"][REGISTRY["default"]]["dim"]
    expected = configure_model.render_schema_text(default_dim)
    assert SCHEMA_FILE.read_text(encoding="utf-8") == expected


def test_committed_schema_vector_dim_matches_default():
    default_dim = REGISTRY["models"][REGISTRY["default"]]["dim"]
    assert f"vector({default_dim})" in SCHEMA_FILE.read_text(encoding="utf-8")


def test_ingestion_embedder_defaults_match_registry_default():
    """The ingestion embedder's fallback constants (used when EMBEDDING_* env is
    unset) must equal the registry default row, so an unconfigured deploy embeds
    with the advertised default model/dim/prompt. Checks the DEFAULT_* literals
    directly, so it holds regardless of what EMBEDDING_* env the suite runs with."""
    from app import embedder

    default_name = REGISTRY["default"]
    default_row = REGISTRY["models"][default_name]
    assert embedder.DEFAULT_MODEL_NAME == default_name
    assert embedder.DEFAULT_EMBEDDING_DIM == default_row["dim"]
    assert embedder.DEFAULT_PASSAGE_PROMPT == default_row["passage_prompt"]


def _extract_function_source(path: Path, func_name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return ast.unparse(node)
    raise AssertionError(f"{func_name} not found in {path}")


def test_addr_is_private_is_byte_identical_across_ssrf_copies():
    """`_addr_is_private` is hand-duplicated in ingestion/app/urlscope.py and
    mcp-server/app/retrieval.py (the two services can't share an import). The
    literal-address classifier must stay identical between them — a divergence
    here is the exact SSRF drift the security review warned about."""
    ing = _extract_function_source(INGESTION_URLSCOPE, "_addr_is_private")
    mcp = _extract_function_source(MCP_RETRIEVAL, "_addr_is_private")
    assert ing == mcp
