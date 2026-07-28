"""Guards that keep `scripts/models_matrix.py` (the per-model container-image
build matrix generator) faithful to its contract with `.github/workflows/
release.yml`: every registry model appears exactly once, slugs are valid and
unique, the registry default sorts first, the JSON row shape/types match what
the workflow's `enable=`/build-arg expressions expect, `--only` filtering and
its error paths behave, and the CLI emits exactly one JSON line on stdout (the
only thing safe to pipe into `$GITHUB_OUTPUT`). Also guards against the
workflow silently reverting to a hardcoded model name instead of consuming
this generator.

Runs under the ingestion venv (see Makefile's `test` target), so PyYAML is
available.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "config" / "models.yaml"
MATRIX_SCRIPT = REPO_ROOT / "scripts" / "models_matrix.py"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


def _load_models_matrix():
    spec = importlib.util.spec_from_file_location("models_matrix", MATRIX_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_registry() -> dict:
    with REGISTRY_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


mm = _load_models_matrix()
REGISTRY = _load_registry()


# Sanity check (development-time only, see task notes): confirms this suite
# is exercising THIS worktree's copy of the code, not some other checkout's.
print(f"[test_models_matrix] REPO_ROOT resolved to: {REPO_ROOT}")
assert REPO_ROOT == Path(__file__).resolve().parent.parent
assert mm.REPO_ROOT == REPO_ROOT


# ---------------------------------------------------------------------------
# 1. Registry coverage
# ---------------------------------------------------------------------------


def test_full_matrix_covers_every_registry_model_exactly_once():
    matrix = mm.build_matrix(REGISTRY, only=None)
    names = [row["model"] for row in matrix]
    assert len(names) == len(REGISTRY["models"])
    assert sorted(names) == sorted(REGISTRY["models"])
    assert len(set(names)) == len(names)  # no duplicates


# ---------------------------------------------------------------------------
# 2. Slug validity and uniqueness
# ---------------------------------------------------------------------------


def test_every_slug_in_the_real_registry_is_valid_and_unique():
    matrix = mm.build_matrix(REGISTRY, only=None)
    slugs = [row["slug"] for row in matrix]
    for slug in slugs:
        assert SLUG_RE.match(slug), f"slug {slug!r} fails {SLUG_RE.pattern!r}"
    assert len(set(slugs)) == len(slugs)


# ---------------------------------------------------------------------------
# 3. Default handling
# ---------------------------------------------------------------------------


def test_exactly_one_default_row_matching_registry_and_it_is_first():
    matrix = mm.build_matrix(REGISTRY, only=None)
    default_rows = [row for row in matrix if row["is_default"]]
    assert len(default_rows) == 1
    assert default_rows[0]["model"] == REGISTRY["default"]
    assert matrix[0]["model"] == REGISTRY["default"]
    assert matrix[0]["is_default"] is True


# ---------------------------------------------------------------------------
# 4. Key/type contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model_name", list(REGISTRY["models"]))
def test_row_has_exact_keys_and_correct_types(model_name):
    matrix = mm.build_matrix(REGISTRY, only=None)
    row = next(r for r in matrix if r["model"] == model_name)
    assert set(row.keys()) == {"model", "slug", "dim", "is_default"}
    assert isinstance(row["dim"], int)
    # bool is a subclass of int in Python, so check bool BEFORE/AS WELL as the
    # dim check to guard against a stringified bool ("true"/"false") which
    # would silently break `enable=` in the workflow while still looking
    # "truthy" in casual inspection.
    assert isinstance(row["is_default"], bool)
    assert not isinstance(row["dim"], bool)
    assert row["dim"] == REGISTRY["models"][model_name]["dim"]


# ---------------------------------------------------------------------------
# 5. Slug derivation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_name, expected_slug",
    [
        ("vendor/Some-Weird_Model.NAME", "some-weird_model.name"),
        ("Vendor/UPPER-CASE", "upper-case"),
        ("no-vendor-prefix", "no-vendor-prefix"),
        ("vendor/name+with*bad!chars", "name-with-bad-chars"),
        ("vendor/deep/nested/path", "path"),
        ("vendor/name with spaces", "name-with-spaces"),
    ],
)
def test_derive_slug_basename_lowercase_and_charset_replacement(model_name, expected_slug):
    assert mm.derive_slug(model_name) == expected_slug


def test_explicit_image_slug_wins_over_derived_slug():
    synthetic_registry = {
        "default": "vendor/model-a",
        "models": {
            "vendor/model-a": {"dim": 100, "image_slug": "custom-slug"},
            "vendor/model-b": {"dim": 200},
        },
    }
    matrix = mm.build_matrix(synthetic_registry, only=None)
    row_a = next(r for r in matrix if r["model"] == "vendor/model-a")
    row_b = next(r for r in matrix if r["model"] == "vendor/model-b")
    assert row_a["slug"] == "custom-slug"  # explicit wins, not derive_slug("vendor/model-a")
    assert row_a["slug"] != mm.derive_slug("vendor/model-a")
    assert row_b["slug"] == mm.derive_slug("vendor/model-b")


# ---------------------------------------------------------------------------
# 6. --only filtering
# ---------------------------------------------------------------------------


def _synthetic_registry() -> dict[str, Any]:
    return {
        "default": "vendor/model-b",
        "models": {
            "vendor/model-a": {"dim": 100},
            "vendor/model-b": {"dim": 200},
            "vendor/model-c": {"dim": 300},
        },
    }


def test_only_single_name_yields_one_row():
    registry = _synthetic_registry()
    matrix = mm.build_matrix(registry, only=mm.parse_only("vendor/model-a"))
    assert [row["model"] for row in matrix] == ["vendor/model-a"]


def test_only_two_names_yields_two_rows():
    registry = _synthetic_registry()
    matrix = mm.build_matrix(registry, only=mm.parse_only("vendor/model-a,vendor/model-c"))
    assert {row["model"] for row in matrix} == {"vendor/model-a", "vendor/model-c"}
    assert len(matrix) == 2


@pytest.mark.parametrize("raw", ["all", "", "ALL", "  ", None])
def test_only_all_empty_or_omitted_yields_full_matrix(raw):
    registry = _synthetic_registry()
    matrix = mm.build_matrix(registry, only=mm.parse_only(raw))
    assert {row["model"] for row in matrix} == set(registry["models"])
    assert len(matrix) == len(registry["models"])


def test_only_ordering_does_not_change_output_ordering():
    registry = _synthetic_registry()
    # default is vendor/model-b; request it last in --only, it must still sort
    # first in the output because ordering follows registry declaration order,
    # default-first, not --only's own order.
    matrix = mm.build_matrix(registry, only=mm.parse_only("vendor/model-c,vendor/model-a,vendor/model-b"))
    assert [row["model"] for row in matrix] == ["vendor/model-b", "vendor/model-a", "vendor/model-c"]


# ---------------------------------------------------------------------------
# 7. Error paths
# ---------------------------------------------------------------------------


def test_unknown_only_name_raises_matrix_error():
    registry = _synthetic_registry()
    with pytest.raises(mm.MatrixError):
        mm.build_matrix(registry, only=["vendor/does-not-exist"])


def test_duplicate_slug_raises_matrix_error():
    registry = {
        "default": "vendor/model-a",
        "models": {
            "vendor/model-a": {"dim": 100, "image_slug": "same-slug"},
            "vendor/model-b": {"dim": 200, "image_slug": "same-slug"},
        },
    }
    with pytest.raises(mm.MatrixError):
        mm.build_matrix(registry, only=None)


def test_invalid_slug_raises_matrix_error():
    registry = {
        "default": "vendor/model-a",
        "models": {
            "vendor/model-a": {"dim": 100, "image_slug": "-leading-dash-invalid"},
        },
    }
    with pytest.raises(mm.MatrixError):
        mm.build_matrix(registry, only=None)


# ---------------------------------------------------------------------------
# 7b. Slug prefix collision (release.yml `download-artifact` glob over-match)
#
# `.github/workflows/release.yml` collects each model's digests with a glob
# `digests-<service>-<slug>-*`. If one row's slug is a literal prefix of
# another row's slug followed by '-', that glob over-matches and the prefix
# model's merge job silently sweeps up the longer-slug model's digests too —
# e.g. `bge-small-en-v1.5` vs `bge-small-en-v1.5-q8` (see task dispatch notes).
# A prefix relationship where the next character is anything other than '-'
# is safe, because the glob's literal '-' cannot match it (e.g.
# `bge-small-en-v1` vs `bge-small-en-v1.5`, where the boundary character is
# '.').
# ---------------------------------------------------------------------------


def test_slug_prefix_followed_by_dash_raises_matrix_error_naming_both_slugs():
    registry = {
        "default": "vendor/model-a",
        "models": {
            "vendor/model-a": {"dim": 100, "image_slug": "bge-small-en-v1.5"},
            "vendor/model-b": {"dim": 200, "image_slug": "bge-small-en-v1.5-q8"},
        },
    }
    with pytest.raises(mm.MatrixError) as exc_info:
        mm.build_matrix(registry, only=None)
    message = str(exc_info.value)
    assert "bge-small-en-v1.5" in message
    assert "bge-small-en-v1.5-q8" in message


@pytest.mark.parametrize(
    "first_slug, second_slug",
    [
        ("bge-small-en-v1.5", "bge-small-en-v1.5-q8"),
        ("bge-small-en-v1.5-q8", "bge-small-en-v1.5"),
    ],
    ids=["shorter-declared-first", "longer-declared-first"],
)
def test_slug_prefix_collision_detected_regardless_of_declaration_order(first_slug, second_slug):
    registry = {
        "default": "vendor/model-a",
        "models": {
            "vendor/model-a": {"dim": 100, "image_slug": first_slug},
            "vendor/model-b": {"dim": 200, "image_slug": second_slug},
        },
    }
    with pytest.raises(mm.MatrixError):
        mm.build_matrix(registry, only=None)


def test_slug_prefix_with_non_dash_boundary_char_is_safe_and_does_not_raise():
    # Regression guard: this is the case a naive "startswith" prefix check
    # would wrongly reject. The glob `...-v1-*` cannot match "...-v1.5" (the
    # boundary character is '.', not '-'), so these two slugs must coexist.
    registry = {
        "default": "vendor/model-a",
        "models": {
            "vendor/model-a": {"dim": 100, "image_slug": "bge-small-en-v1"},
            "vendor/model-b": {"dim": 200, "image_slug": "bge-small-en-v1.5"},
        },
    }
    matrix = mm.build_matrix(registry, only=None)
    assert {row["slug"] for row in matrix} == {"bge-small-en-v1", "bge-small-en-v1.5"}


def test_identical_slugs_raise_the_duplicate_slug_error_not_the_prefix_error():
    # Same string is not a "prefix followed by '-'" of itself (there is no
    # boundary character at all), so an exact duplicate must still be caught
    # by the pre-existing duplicate-slug check, not silently pass through
    # the new prefix-collision check.
    registry = {
        "default": "vendor/model-a",
        "models": {
            "vendor/model-a": {"dim": 100, "image_slug": "same-slug"},
            "vendor/model-b": {"dim": 200, "image_slug": "same-slug"},
        },
    }
    with pytest.raises(mm.MatrixError, match="both resolve to slug"):
        mm.build_matrix(registry, only=None)


def test_real_registry_slugs_are_mutually_prefix_free():
    # Guards config/models.yaml itself: a future row whose slug is a prefix
    # of an existing slug followed by '-' (or vice versa) must fail here,
    # not surface as a broken release.yml artifact download in CI.
    matrix = mm.build_matrix(REGISTRY, only=None)
    slugs = [row["slug"] for row in matrix]
    for i, slug_a in enumerate(slugs):
        for slug_b in slugs[i + 1 :]:
            shorter, longer = sorted((slug_a, slug_b), key=len)
            assert not longer.startswith(shorter + "-"), (
                f"slug {shorter!r} is a prefix of {longer!r} followed by '-' — "
                "release.yml's download-artifact glob would over-match"
            )


def test_main_returns_exit_code_2_and_writes_nothing_to_stdout_for_unknown_only_name(
    capsys, monkeypatch
):
    # Point load_registry at a synthetic registry so this test doesn't depend
    # on any particular real model name being "unknown".
    registry = _synthetic_registry()
    monkeypatch.setattr(mm, "load_registry", lambda: registry)
    exit_code = mm.main(["models_matrix.py", "--only", "vendor/does-not-exist"])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err.strip() != ""


# ---------------------------------------------------------------------------
# 8. stdout shape
# ---------------------------------------------------------------------------


def test_cli_stdout_is_exactly_one_json_line_that_round_trips():
    result = subprocess.run(
        [sys.executable, str(MATRIX_SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed == mm.build_matrix(REGISTRY, only=None)


# ---------------------------------------------------------------------------
# 9. Anti-hardcoding guard: release.yml must derive its build matrix from
# this generator instead of hardcoding a registry model name/vendor prefix,
# so adding/removing a model row can never silently drift out of sync with
# the workflow.
# ---------------------------------------------------------------------------


def _forbidden_substrings_from_registry() -> list[str]:
    """Every full model name and its vendor prefix (text before the last
    '/'), built from the registry file itself so this guard keeps working
    unmodified when a model row is added or removed."""
    forbidden: set[str] = set()
    for name in REGISTRY["models"]:
        forbidden.add(name)
        if "/" in name:
            forbidden.add(name.split("/", 1)[0])
    return sorted(forbidden)


def test_release_workflow_does_not_hardcode_a_registry_model_name_or_vendor():
    workflow_text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    offenders = [s for s in _forbidden_substrings_from_registry() if s in workflow_text]
    assert offenders == [], (
        "release.yml hardcodes registry model name(s)/vendor prefix(es) "
        f"{offenders!r} instead of deriving the build matrix from "
        "scripts/models_matrix.py"
    )
