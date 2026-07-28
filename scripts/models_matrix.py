#!/usr/bin/env python3
"""Generate the per-model container-image build matrix from the registry.

`config/models.yaml` is the single source of truth for embedding models (see
its header comment). `.github/workflows/release.yml` needs one build job per
model — each image bakes in a different `EMBEDDING_MODEL_NAME` build arg and
gets its own GHCR tag suffix — but the workflow itself must not hardcode that
list, or adding a model row would silently NOT get an image until someone
remembered to edit the YAML by hand.

This script closes that gap: the workflow shells out to it (its stdout is
captured straight into `$GITHUB_OUTPUT`, so nothing else may print to stdout)
and gets back one JSON array with one row per model, ordered registry-default
first, then `config/models.yaml` declaration order. Each row carries:

    model       the full FastEmbed model name (used as the build arg)
    slug        a GHCR-tag-safe suffix (each model's row of image tags is
                suffixed `-<slug>`, e.g. self-docs-ingestion-bge-base-en-v1.5)
    dim         the model's vector dimension (informational for the workflow)
    is_default  true for exactly the row matching `default:` in the registry

`--only <csv>` restricts the matrix to a subset of full model names (or the
literal `all`/an empty string, meaning "every model") — used by the release
workflow to build the default model on every push while gating the rest
behind a manual dispatch input, without duplicating this generator's logic.

Usage:
    python scripts/models_matrix.py [--only <csv>]

Exit codes: 0 with the JSON array on stdout; 2 with a message on stderr (and
NOTHING on stdout) if --only names an unknown model, two rows resolve to the
same slug, two rows' slugs collide via the artifact-download glob (one slug
is the other plus a '-' prefix, e.g. `foo` and `foo-bar`), or a slug is not a
valid Docker tag fragment.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover - environment hint
    raise SystemExit(
        "models_matrix.py needs PyYAML. Install it with `pip install pyyaml`, "
        "or run this via the ingestion venv (which already has it): "
        "ingestion/.venv/bin/python scripts/models_matrix.py"
    ) from exc

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "config" / "models.yaml"

# A valid Docker image tag fragment: this is appended to a fixed prefix
# (`self-docs-<service>-`), so it need not satisfy the FULL tag grammar on its
# own, but it still must never contain characters Docker tags reject.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SLUG_INVALID_CHARS = re.compile(r"[^a-z0-9._-]")


class MatrixError(Exception):
    """A registry/`--only` problem that should exit 2 with a stderr message,
    as opposed to letting an unrelated exception produce a stray traceback."""


def load_registry() -> dict[str, Any]:
    with REGISTRY_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)  # type: ignore[no-any-return]


def derive_slug(model_name: str) -> str:
    """Fallback slug for a row with no explicit `image_slug:`: the basename
    after the last '/', lowercased, with every character outside
    [a-z0-9._-] replaced by '-'."""
    basename = model_name.rsplit("/", 1)[-1].lower()
    return _SLUG_INVALID_CHARS.sub("-", basename)


def parse_only(raw: str | None) -> list[str] | None:
    """Normalize --only's raw string into a list of model names, or None
    meaning "every model" (its `all`/empty-string/omitted spellings)."""
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped or stripped.lower() == "all":
        return None
    return [name.strip() for name in stripped.split(",") if name.strip()]


def build_matrix(registry: dict[str, Any], only: list[str] | None = None) -> list[dict[str, Any]]:
    """Pure function: registry dict (as loaded from config/models.yaml) + an
    optional --only selection -> the ordered matrix rows. Raises MatrixError
    (never exits/prints) so tests and callers can handle failures themselves.
    """
    models: dict[str, Any] = registry["models"]
    default_name: str = registry["default"]

    if only is not None:
        unknown = [name for name in only if name not in models]
        if unknown:
            available = "\n  ".join(sorted(models))
            raise MatrixError(
                f"--only names unknown model(s): {', '.join(unknown)}. "
                f"Valid models (config/models.yaml):\n  {available}"
            )
        wanted = set(only)
        selected_names = [name for name in models if name in wanted]
    else:
        selected_names = list(models)

    # Registry-default first (if selected), then config/models.yaml
    # declaration order for the rest — independent of --only's own order.
    ordered_names = [name for name in selected_names if name == default_name] + [
        name for name in selected_names if name != default_name
    ]

    rows: list[dict[str, Any]] = []
    slug_owners: dict[str, str] = {}
    for name in ordered_names:
        row = models[name]
        slug = row.get("image_slug") or derive_slug(name)
        if not SLUG_RE.match(slug):
            raise MatrixError(
                f"model {name!r} resolves to slug {slug!r}, which is not a valid "
                f"image tag fragment (must match {SLUG_RE.pattern!r})"
            )
        if slug in slug_owners:
            raise MatrixError(
                f"slug collision: {slug_owners[slug]!r} and {name!r} both resolve "
                f"to slug {slug!r} — set an explicit, unique `image_slug:` on one"
            )
        # Beyond exact duplicates, also reject a slug that is another
        # selected slug plus a literal '-' prefix (e.g. `foo` / `foo-bar`):
        # `.github/workflows/release.yml`'s merge job downloads a
        # (service, model)'s digests with
        # `pattern: digests-<service>-<slug>-*`, so `foo-*` would ALSO match
        # `digests-<service>-foo-bar-amd64`/`-arm64` and fuse the wrong
        # model's platform digests into one manifest list. A slug that is
        # merely a prefix without the trailing '-' (e.g. `bge-small-en-v1`
        # vs `bge-small-en-v1.5`) is safe: the glob's literal '-' cannot
        # match the following '.'.
        #
        # Scoping this to `ordered_names`/`slug_owners` (the already-
        # selected set), same as the exact-duplicate check just above, is
        # deliberate, not an oversight: the glob only ever collects
        # artifacts uploaded by build legs that ran IN THIS SAME workflow
        # run, and `build`/`merge` only ever iterate
        # `fromJSON(needs.prepare.outputs.models)` — i.e. exactly this
        # function's --only-selected rows. A model excluded by --only never
        # uploads a `digests-...` artifact at all in that run, so it can
        # never collide with anything, and rejecting it anyway (by
        # scanning the full registry regardless of --only) would block a
        # legitimate `--only=foo` dispatch just because some unrelated,
        # unselected `foo-bar` row exists in the registry. For the same
        # reason, the exact-duplicate check above is already correctly
        # scoped and does not need widening for this defect.
        for other_slug, other_name in slug_owners.items():
            if slug.startswith(other_slug + "-") or other_slug.startswith(slug + "-"):
                raise MatrixError(
                    f"slug glob collision: {other_name!r} (slug {other_slug!r}) and "
                    f"{name!r} (slug {slug!r}) — one slug is the other plus a "
                    f"'-' prefix, so the merge job's artifact download glob "
                    f"'digests-<service>-<slug>-*' would match both models' "
                    f"digests and publish a manifest with the wrong platforms; "
                    f"set a distinct, explicit `image_slug:` on one to fix it"
                )
        slug_owners[slug] = name
        rows.append(
            {
                "model": name,
                "slug": slug,
                "dim": int(row["dim"]),
                "is_default": name == default_name,
            }
        )
    return rows


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="models_matrix.py",
        description="Print the per-model container-image build matrix as one line of JSON.",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="Comma-separated full model names to include, or 'all'/empty for every model.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv[1:])
    registry = load_registry()
    only = parse_only(args.only)
    try:
        matrix = build_matrix(registry, only)
    except MatrixError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(matrix))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
