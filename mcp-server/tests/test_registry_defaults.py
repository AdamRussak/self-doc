"""Guards that the mcp-server's runtime embedding defaults match the shared
model registry (config/models.yaml). The ingestion side and the schema/SSRF
parity live in the top-level tests/test_model_registry.py; this covers the
retrieval service's own fallback constants.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REGISTRY_PATH = REPO_ROOT / "config" / "models.yaml"


def _load_registry() -> dict:
    with REGISTRY_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_retrieval_defaults_match_registry_default():
    """The retrieval service's fallback constants (used when EMBEDDING_* env is
    unset) must equal the registry default row. Checks the DEFAULT_* literals
    directly so it holds regardless of the suite's EMBEDDING_* env, and avoids
    reloading the module (which would re-register its Prometheus collectors)."""
    from app import retrieval

    registry = _load_registry()
    default_name = registry["default"]
    default_row = registry["models"][default_name]
    assert retrieval.DEFAULT_MODEL_NAME == default_name
    assert retrieval.DEFAULT_QUERY_PROMPT == default_row["query_prompt"]
