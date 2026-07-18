"""Pure helpers for URL scoping and sitemap parsing.

No network, no filesystem, no DB access — these functions operate purely on
their inputs. They exist to fix two production defects:

1. Scope leak: prefix matching via plain `str.startswith` lets an
   `include_prefixes` entry like `/traefik` also match `/traefik-hub/...`,
   `/traefik-enterprise/...`, `/traefik-mesh/...` — different products that
   merely share a string prefix. `path_allowed` matches at path-segment
   boundaries instead (a prefix must be followed by `/` or end-of-string).

2. Sitemap index not handled: a sitemap.xml can itself be a `<sitemapindex>`
   whose children are `<sitemap><loc>` entries pointing at OTHER sitemap.xml
   files, rather than a `<urlset>` of `<url><loc>` page URLs. `parse_sitemap`
   distinguishes the two and returns both possible lists so a caller can
   recurse into child sitemaps.

INCLUDE vs EXCLUDE matching is deliberately ASYMMETRIC — read this before
adding a new source's include_prefixes/exclude_prefixes:

- `include_prefixes` use STRICT path-segment-boundary matching (a prefix
  must be followed by '/' or end-of-string). This is load-bearing: a loose
  substring match here is exactly the traefik/traefik-hub scope-leak bug
  this module fixes. Over-matching an include silently admits an entire
  wrong product's docs into the corpus.
- `exclude_prefixes` use plain substring/startswith matching (no boundary
  requirement) — this restores the crawler's original pre-fix behavior for
  excludes specifically. Reasoning: exclude prefixes routinely need to
  filter a whole family of *versioned* segments — e.g. `/traefik/v1` must
  reject `/traefik/v1.7/basics` and `/traefik/v2` must reject
  `/traefik/v2.11/routing` — where boundary matching would require the
  config to enumerate every real minor version (`v1.0`, `v1.7`, `v2.0`,
  `v2.5`, `v2.10`, `v2.11`, ...), which is brittle and silently rots as new
  versions ship. The failure mode of over-matching an exclude is safe
  (fails closed: a legitimately-wanted page is skipped) whereas the failure
  mode of over-matching an include is unsafe (fails open: an entire wrong
  product is ingested). That asymmetric risk is why the two keys get
  different matching semantics instead of forcing symmetric boundary rules
  everywhere.
- `exclude_prefixes` always wins over `include_prefixes` regardless of
  which matching rule fired.
- A trailing slash on a configured prefix (include OR exclude) is
  normalized away before matching, so '/latest' and '/latest/' behave
  identically for both keys — e.g. `exclude_prefixes: ["/compose/releases/"]`
  excludes the bare path `/compose/releases` itself, not just its children.
  Only the BOUNDARY REQUIREMENT differs between include and exclude, not
  trailing-slash handling.
"""

from __future__ import annotations

from xml.etree import ElementTree

_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def _normalize_prefix(prefix: str) -> str:
    """Strip a trailing slash (except for the root '/') and ensure a
    leading slash, so '/latest' and '/latest/' are treated identically."""
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    if len(prefix) > 1 and prefix.endswith("/"):
        prefix = prefix.rstrip("/")
    return prefix


def _matches_prefix(path: str, prefix: str) -> bool:
    """True if `path` matches `prefix` at a path-segment boundary: exact
    match, or `prefix` followed immediately by '/' in `path`."""
    prefix = _normalize_prefix(prefix)
    if prefix == "/":
        return True
    if path == prefix:
        return True
    return path.startswith(prefix + "/")


def _matches_exclude_prefix(path: str, prefix: str) -> bool:
    """Plain substring/startswith match — NOT boundary-anchored. See the
    module docstring for why excludes intentionally use looser matching
    than includes (version-segment filtering, e.g. '/traefik/v2' must
    reject '/traefik/v2.11/routing').

    The prefix IS normalized (leading slash ensured, trailing slash
    stripped) before matching, same as `_matches_prefix`, so a configured
    exclude of e.g. '/compose/releases/' also excludes the bare path
    '/compose/releases' itself, not just its children — a trailing slash
    in config must not change whether the top-level page itself is
    excluded."""
    prefix = _normalize_prefix(prefix)
    return path.startswith(prefix)


def path_allowed(path: str, include_prefixes: list[str], exclude_prefixes: list[str]) -> bool:
    """True if `path` is allowed under the include/exclude prefix rules.

    `include_prefixes` match at path-segment boundaries (see
    `_matches_prefix`), not a bare `str.startswith`, so `/traefik` does not
    accidentally match `/traefik-hub/...`. `exclude_prefixes` match via
    plain substring/startswith (see `_matches_exclude_prefix` and the
    module docstring for why) and always win over `include_prefixes`. An
    empty `include_prefixes` list means "allow all" (subject to excludes).
    """
    if any(_matches_exclude_prefix(path, p) for p in exclude_prefixes):
        return False
    if include_prefixes:
        return any(_matches_prefix(path, p) for p in include_prefixes)
    return True


def _strip_ns(tag: str) -> str:
    """Strip an XML namespace URI wrapper (e.g. '{ns}tag' -> 'tag')."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def parse_sitemap(xml_bytes: bytes) -> tuple[list[str], list[str]]:
    """Parse sitemap XML bytes into `(urls, child_sitemaps)`.

    - If the root element is `<sitemapindex>`, its `<sitemap><loc>` values
      are returned as `child_sitemaps` and `urls` is empty.
    - Otherwise (a `<urlset>`), its `<url><loc>` values are returned as
      `urls` and `child_sitemaps` is empty.

    Handles both namespaced (`xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"`)
    and non-namespaced XML. `<loc>` text is stripped of surrounding
    whitespace. Raises `ValueError` on malformed or empty input.
    """
    if not xml_bytes:
        raise ValueError("parse_sitemap: empty input")
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as e:
        raise ValueError(f"parse_sitemap: malformed XML: {e}") from e

    root_tag = _strip_ns(root.tag)

    if root_tag == "sitemapindex":
        child_sitemaps = [
            el.text.strip()
            for el in root.findall(".//sm:sitemap/sm:loc", _SITEMAP_NS)
            if el.text and el.text.strip()
        ]
        if not child_sitemaps:
            child_sitemaps = [
                el.text.strip()
                for el in root.findall(".//sitemap/loc")
                if el.text and el.text.strip()
            ]
        return [], child_sitemaps

    if root_tag == "urlset":
        urls = [
            el.text.strip()
            for el in root.findall(".//sm:url/sm:loc", _SITEMAP_NS)
            if el.text and el.text.strip()
        ]
        if not urls:
            urls = [
                el.text.strip()
                for el in root.findall(".//url/loc")
                if el.text and el.text.strip()
            ]
        return urls, []

    raise ValueError(f"parse_sitemap: unexpected root element <{root_tag}>, expected <urlset> or <sitemapindex>")
