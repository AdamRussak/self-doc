"""Computed defaults for URL-only doc-source creation.

Context (see `urlscope.py` module docstring for the include/exclude
asymmetry this module must respect): a source config with
`include_prefixes = []` allows the ENTIRE host (`urlscope.path_allowed`
returns True on an empty include list), and `max_pages = None` maps to no
page ceiling at all (`crawler.py`). Combined, a URL-only source on a shared
docs host (e.g. `doc.traefik.io` hosting both `/traefik` and
`/traefik-hub`, or `docs.docker.com` hosting many products) would crawl the
entire site uncapped — same-host is the only remaining bound.

To make "just paste a URL" safe, defaults are COMPUTED at creation time
instead of left empty:

- `derive_name`: a `^[a-z0-9-]+$` slug from the host + first meaningful
  path segment, with collision suffixes (`-2`, `-3`, ...).
- `derive_include_prefixes`: `[path]` when `base_url` has a non-root path
  (scoping the crawl to that one path, boundary-safe per
  `urlscope._matches_prefix`), or `[]` for a root docs site — in which case
  `DEFAULT_MAX_PAGES` becomes the only bound.
- `apply_creation_defaults`: fills blanks only; any explicitly-supplied
  value (including an explicit "unlimited" `max_pages=None` choice) always
  wins.

Pure functions only — no network, no filesystem, no DB access.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from urllib.parse import urlparse

DEFAULT_MAX_PAGES = 500  # modal value across the 40 live sources

# Host labels that carry no product-identifying information and should be
# stripped before deriving a name from the host.
_GENERIC_HOST_LABELS = frozenset(
    {
        "www",
        "docs",
        "doc",
        "developers",
        "developer",
        "platform",
        "api",
    }
)

# Path segments that carry no product-identifying information and should be
# skipped when looking for the "first meaningful path segment".
_GENERIC_PATH_SEGMENTS = frozenset(
    {
        "docs",
        "doc",
        "en",
        "api",
        "reference",
        "guide",
    }
)

_NAME_PATTERN = re.compile(r"^[a-z0-9-]+$")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _slugify(label: str) -> str:
    """Lowercase `label` and collapse any run of non-alphanumeric characters
    into a single '-', stripping leading/trailing '-'. Empty input -> ''."""
    label = _NON_ALNUM.sub("-", label.lower()).strip("-")
    return label


def derive_name(base_url: str, taken: Collection[str]) -> str:
    """Derive a `^[a-z0-9-]+$` slug for a new source from its `base_url`,
    made unique against `taken` (which must include names from EVERY
    status, not just active sources — a `rejected` row still holds its
    name) by appending `-2`, `-3`, ... on collision.

    Strategy:

    1. `host_identity` = the first dot-separated host label that is NOT a
       generic label (`_GENERIC_HOST_LABELS`); everything after it
       (including the TLD) is ignored. E.g. `doc.traefik.io` -> `traefik`
       (skip `doc`, ignore `.io`); `developers.google.com` -> `google`
       (skip `developers`, ignore `.com`); `fastapi.tiangolo.com` ->
       `fastapi` (nothing generic to skip, ignore `.tiangolo.com`).
    2. `first_segment` = the first non-empty path segment that is NOT a
       generic segment (`_GENERIC_PATH_SEGMENTS`), or `None` if there is
       none.
    3. If `first_segment` is `None` or equal (case-insensitively) to
       `host_identity`, the name is `host_identity` alone. Otherwise the
       name is `{host_identity}-{first_segment}` (e.g. `google-maps`).
    """
    parsed = urlparse(base_url)
    host = parsed.hostname or ""
    host_labels = [lbl for lbl in host.split(".") if lbl]
    non_generic_host_labels = [lbl for lbl in host_labels if lbl.lower() not in _GENERIC_HOST_LABELS]
    candidate_labels = non_generic_host_labels or host_labels
    host_identity = _slugify(candidate_labels[0]) if candidate_labels else ""

    path_segments = [seg for seg in (parsed.path or "").split("/") if seg]
    first_segment = next(
        (
            _slugify(seg)
            for seg in path_segments
            if seg.lower() not in _GENERIC_PATH_SEGMENTS and _slugify(seg)
        ),
        None,
    )

    if not first_segment or first_segment == host_identity:
        base_name = host_identity or first_segment or "source"
    elif host_identity:
        base_name = f"{host_identity}-{first_segment}"
    else:
        base_name = first_segment

    base_name = _slugify(base_name) or "source"
    if not _NAME_PATTERN.match(base_name):
        base_name = "source"

    if base_name not in taken:
        return base_name

    n = 2
    while f"{base_name}-{n}" in taken:
        n += 1
    return f"{base_name}-{n}"


def derive_include_prefixes(base_url: str) -> list[str]:
    """`[path]` when `base_url`'s path is non-root, else `[]`.

    A non-root path scopes the crawl to that path only (boundary-safe via
    `urlscope._matches_prefix` at crawl time — deriving `/traefik` will not
    admit `/traefik-hub`). A root path yields `[]` (allow the whole host),
    relying on `DEFAULT_MAX_PAGES` as the only remaining bound.
    """
    path = urlparse(base_url).path
    normalized = path.rstrip("/")
    if not normalized:
        return []
    return [normalized]


def apply_creation_defaults(fields: dict, taken: Collection[str]) -> dict:
    """Return a copy of `fields` with blanks filled by computed defaults.

    Fills ONLY when a field is absent, `None`, or (for string fields) an
    empty/whitespace string. `include_prefixes: []` and `max_pages: None`
    are NOT considered blank when the caller supplied them explicitly by
    including the key with that exact value — callers that want an
    "unlimited"/"allow whole host" outcome must therefore not include the
    key at all when they mean "please compute a default for me", and CAN
    pass the key explicitly to opt out of the computed default. Concretely:

    - `name`: filled when missing/None/empty-after-strip.
    - `include_prefixes`: filled ONLY when the key is absent (not merely
      an empty list), so an explicit `include_prefixes: []` (meaning "I
      really do want the whole host") is preserved.
    - `max_pages`: filled ONLY when the key is absent (not merely `None`),
      so an explicit `max_pages: None` (meaning "unlimited, I understand
      the risk") is preserved.

    `base_url` is required in `fields` and is never modified.
    """
    result = dict(fields)
    base_url = result.get("base_url")
    if not base_url:
        raise ValueError("apply_creation_defaults: 'base_url' is required")

    name = result.get("name")
    if name is None or (isinstance(name, str) and not name.strip()):
        result["name"] = derive_name(str(base_url), taken)

    if "include_prefixes" not in result:
        result["include_prefixes"] = derive_include_prefixes(str(base_url))

    if "max_pages" not in result:
        result["max_pages"] = DEFAULT_MAX_PAGES

    return result
