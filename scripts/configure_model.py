#!/usr/bin/env python3
"""Resolve an embedding model selection into deploy config.

`make configure MODEL=<name>` (or `python scripts/configure_model.py <name>`)
reads config/models.yaml — the single source of truth — and, for the chosen
model:

  1. Upserts these keys into .env (creating it if absent, preserving every
     other key already there):
        EMBEDDING_MODEL_NAME, EMBEDDING_DIM,
        EMBEDDING_QUERY_PROMPT, EMBEDDING_PASSAGE_PROMPT,
        INGESTION_MEM_LIMIT, MCP_MEM_LIMIT
     docker-compose interpolates the memory limits; the two services read the
     rest at runtime.
  2. Renders db/init/01_schema.sql from 01_schema.sql.template with the model's
     vector dimension.

No model name is passed => the registry default is used. The services default
to the same registry-default values when a key is unset, so this step is only
required to (a) deviate from the default or (b) regenerate .env/schema.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover - environment hint
    raise SystemExit(
        "configure_model.py needs PyYAML. Install it with `pip install pyyaml`, "
        "or run this via the ingestion venv (which already has it): "
        "ingestion/.venv/bin/python scripts/configure_model.py <model>"
    ) from exc

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "config" / "models.yaml"
ENV_PATH = REPO_ROOT / ".env"
SCHEMA_TEMPLATE = REPO_ROOT / "db" / "init" / "01_schema.sql.template"
SCHEMA_OUT = REPO_ROOT / "db" / "init" / "01_schema.sql"

DIM_PLACEHOLDER = "__EMBEDDING_DIM__"

# .env keys this script owns. Upserted on every run; everything else is left
# untouched.
MANAGED_KEYS = (
    "EMBEDDING_MODEL_NAME",
    "EMBEDDING_DIM",
    "EMBEDDING_QUERY_PROMPT",
    "EMBEDDING_PASSAGE_PROMPT",
    "INGESTION_MEM_LIMIT",
    "MCP_MEM_LIMIT",
)


def load_registry() -> dict:
    with REGISTRY_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def resolve(registry: dict, model: str | None) -> tuple[str, dict]:
    models = registry["models"]
    name = model or registry["default"]
    if name not in models:
        available = "\n  ".join(sorted(models))
        raise SystemExit(
            f"unknown model {name!r}. Supported models (config/models.yaml):\n  {available}"
        )
    return name, models[name]


def derived_env(name: str, row: dict) -> dict[str, str]:
    return {
        "EMBEDDING_MODEL_NAME": name,
        "EMBEDDING_DIM": str(row["dim"]),
        "EMBEDDING_QUERY_PROMPT": row.get("query_prompt", ""),
        "EMBEDDING_PASSAGE_PROMPT": row.get("passage_prompt", ""),
        "INGESTION_MEM_LIMIT": row["mem_ingestion"],
        "MCP_MEM_LIMIT": row["mem_mcp"],
    }


def _quote(value: str) -> str:
    """Quote a value for .env if it contains spaces or is empty, so
    docker-compose / the Makefile's `-include .env` parse it as one token."""
    if value == "" or any(c in value for c in " \t#'\""):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def upsert_env(updates: dict[str, str]) -> None:
    """Update-or-append MANAGED_KEYS in .env, preserving all other lines."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        key = stripped.split("=", 1)[0].strip() if ("=" in stripped and not stripped.startswith("#")) else None
        if key in updates:
            out.append(f"{key}={_quote(updates[key])}")
            seen.add(key)
        else:
            out.append(line)
    trailing = [k for k in MANAGED_KEYS if k in updates and k not in seen]
    if trailing:
        if out and out[-1].strip():
            out.append("")
        out.append("# --- Embedding model (managed by scripts/configure_model.py) ---")
        out.extend(f"{k}={_quote(updates[k])}" for k in trailing)
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


def render_schema_text(dim: int) -> str:
    """Pure render of db/init/01_schema.sql for a given vector dimension. Kept
    side-effect-free so tests can assert the committed file matches this output
    (tests/test_model_registry.py)."""
    template = SCHEMA_TEMPLATE.read_text(encoding="utf-8")
    # Swap the TEMPLATE header for a GENERATED one FIRST — the template header
    # text itself contains the __EMBEDDING_DIM__ token, so this must happen
    # before the dimension substitution below or the match would break.
    rendered = template.replace(
        "-- TEMPLATE — do not edit db/init/01_schema.sql by hand.\n"
        "--\n"
        "-- scripts/configure_model.py renders this file into db/init/01_schema.sql,\n"
        "-- substituting __EMBEDDING_DIM__ with the selected model's vector dimension\n"
        "-- (see config/models.yaml). The committed 01_schema.sql is the rendering for\n"
        "-- the registry's default model; `make configure MODEL=<name>` re-renders it.\n"
        "-- A parity test (tests/test_model_registry.py) fails CI if the two drift.",
        "-- GENERATED from db/init/01_schema.sql.template by scripts/configure_model.py.\n"
        "-- Do not edit by hand — run `make configure MODEL=<name>` to change the vector\n"
        f"-- dimension. Rendered for embedding dimension {dim}.",
    )
    return rendered.replace(DIM_PLACEHOLDER, str(dim))


def render_schema(dim: int) -> None:
    SCHEMA_OUT.write_text(render_schema_text(dim), encoding="utf-8")


def main(argv: list[str]) -> int:
    model = argv[1] if len(argv) > 1 and argv[1] else None
    registry = load_registry()
    name, row = resolve(registry, model)
    env = derived_env(name, row)
    upsert_env(env)
    render_schema(int(row["dim"]))
    print(f"Configured embedding model: {name}")
    print(f"  dim               = {env['EMBEDDING_DIM']}")
    print(f"  query_prompt      = {env['EMBEDDING_QUERY_PROMPT']!r}")
    print(f"  passage_prompt    = {env['EMBEDDING_PASSAGE_PROMPT']!r}")
    print(f"  ingestion memory  = {env['INGESTION_MEM_LIMIT']}")
    print(f"  mcp-server memory = {env['MCP_MEM_LIMIT']}")
    print(f"Wrote {ENV_PATH.relative_to(REPO_ROOT)} and rendered {SCHEMA_OUT.relative_to(REPO_ROOT)}.")
    print("Changing model invalidates existing vectors — run `make reindex` to re-embed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
