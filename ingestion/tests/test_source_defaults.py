"""Unit tests for `app.source_defaults` — pure functions, no network/DB."""

from __future__ import annotations

from app.source_defaults import (
    DEFAULT_MAX_PAGES,
    apply_creation_defaults,
    derive_include_prefixes,
    derive_name,
)
from app.urlscope import _matches_prefix

NAME_PATTERN_RE = __import__("re").compile(r"^[a-z0-9-]+$")


# ---------------------------------------------------------------------------
# derive_name
# ---------------------------------------------------------------------------


def test_derive_name_traefik():
    assert derive_name("https://doc.traefik.io/traefik/", set()) == "traefik"


def test_derive_name_google_maps():
    assert derive_name("https://developers.google.com/maps/documentation", set()) == "google-maps"


def test_derive_name_fastapi_root():
    assert derive_name("https://fastapi.tiangolo.com/", set()) == "fastapi"


def test_derive_name_matches_pattern():
    for url in (
        "https://doc.traefik.io/traefik/",
        "https://developers.google.com/maps/documentation",
        "https://fastapi.tiangolo.com/",
        "https://docs.docker.com/compose/",
        "https://api.example.com/v2/widgets",
    ):
        name = derive_name(url, set())
        assert NAME_PATTERN_RE.match(name), f"{name!r} for {url!r} does not match name pattern"


def test_derive_name_collision_suffix():
    taken = {"traefik"}
    assert derive_name("https://doc.traefik.io/traefik/", taken) == "traefik-2"


def test_derive_name_collision_suffix_skips_taken_numbers():
    taken = {"traefik", "traefik-2", "traefik-3"}
    assert derive_name("https://doc.traefik.io/traefik/", taken) == "traefik-4"


def test_derive_name_taken_must_include_rejected_rows():
    # Caller responsibility, exercised here: a name held by a `rejected` row
    # (passed in via `taken`) must still trigger the collision suffix.
    taken = {"traefik"}  # simulates a rejected row's name
    assert derive_name("https://doc.traefik.io/traefik/", taken) != "traefik"


# ---------------------------------------------------------------------------
# derive_include_prefixes
# ---------------------------------------------------------------------------


def test_derive_include_prefixes_root_path_is_empty():
    assert derive_include_prefixes("https://fastapi.tiangolo.com/") == []
    assert derive_include_prefixes("https://fastapi.tiangolo.com") == []


def test_derive_include_prefixes_non_root_path():
    assert derive_include_prefixes("https://doc.traefik.io/traefik/") == ["/traefik"]


def test_derive_include_prefixes_boundary_safe_against_similar_product():
    prefixes = derive_include_prefixes("https://doc.traefik.io/traefik/")
    assert prefixes == ["/traefik"]
    # This is the exact scope-leak bug urlscope.py exists to prevent: the
    # derived prefix must not admit a same-prefixed sibling product.
    assert _matches_prefix("/traefik-hub/intro", prefixes[0]) is False
    assert _matches_prefix("/traefik/routing", prefixes[0]) is True
    assert _matches_prefix("/traefik", prefixes[0]) is True


def test_derive_include_prefixes_nested_path():
    assert derive_include_prefixes("https://developers.google.com/maps/documentation") == [
        "/maps/documentation"
    ]


# ---------------------------------------------------------------------------
# apply_creation_defaults
# ---------------------------------------------------------------------------


def test_apply_creation_defaults_fills_all_blanks():
    fields = {"base_url": "https://doc.traefik.io/traefik/"}
    result = apply_creation_defaults(fields, taken=set())
    assert result["name"] == "traefik"
    assert result["include_prefixes"] == ["/traefik"]
    assert result["max_pages"] == DEFAULT_MAX_PAGES


def test_apply_creation_defaults_root_path_no_bound_but_capped_by_max_pages():
    fields = {"base_url": "https://fastapi.tiangolo.com/"}
    result = apply_creation_defaults(fields, taken=set())
    assert result["include_prefixes"] == []
    assert result["max_pages"] == DEFAULT_MAX_PAGES


def test_apply_creation_defaults_explicit_name_wins():
    fields = {"base_url": "https://doc.traefik.io/traefik/", "name": "custom-name"}
    result = apply_creation_defaults(fields, taken=set())
    assert result["name"] == "custom-name"


def test_apply_creation_defaults_blank_name_is_filled():
    fields = {"base_url": "https://doc.traefik.io/traefik/", "name": "  "}
    result = apply_creation_defaults(fields, taken=set())
    assert result["name"] == "traefik"


def test_apply_creation_defaults_explicit_include_prefixes_preserved():
    fields = {
        "base_url": "https://doc.traefik.io/traefik/",
        "include_prefixes": ["/traefik/v2"],
    }
    result = apply_creation_defaults(fields, taken=set())
    assert result["include_prefixes"] == ["/traefik/v2"]


def test_apply_creation_defaults_explicit_empty_include_prefixes_preserved():
    # An explicit empty list means "I really do want the whole host" and
    # must not be overwritten by the computed default.
    fields = {"base_url": "https://doc.traefik.io/traefik/", "include_prefixes": []}
    result = apply_creation_defaults(fields, taken=set())
    assert result["include_prefixes"] == []


def test_apply_creation_defaults_explicit_max_pages_preserved():
    fields = {"base_url": "https://doc.traefik.io/traefik/", "max_pages": 50}
    result = apply_creation_defaults(fields, taken=set())
    assert result["max_pages"] == 50


def test_apply_creation_defaults_explicit_unlimited_max_pages_preserved():
    # An explicit `max_pages: None` ("unlimited, I understand the risk")
    # must be preserved, not silently replaced by DEFAULT_MAX_PAGES.
    fields = {"base_url": "https://doc.traefik.io/traefik/", "max_pages": None}
    result = apply_creation_defaults(fields, taken=set())
    assert result["max_pages"] is None


def test_apply_creation_defaults_requires_base_url():
    import pytest

    with pytest.raises(ValueError):
        apply_creation_defaults({}, taken=set())


def test_apply_creation_defaults_does_not_mutate_input():
    fields = {"base_url": "https://doc.traefik.io/traefik/"}
    original = dict(fields)
    apply_creation_defaults(fields, taken=set())
    assert fields == original
