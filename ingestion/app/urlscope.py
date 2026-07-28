"""Pure helpers for URL scoping, canonicalization, and sitemap parsing.

No network, no filesystem, no DB access — these functions operate purely on
their inputs. They exist to fix three production defects:

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

3. Locale/tracking query-parameter explosion: include/exclude prefixes only
   ever match `urlparse(url).path`, so NO source config could express "don't
   crawl the translated copies." Sites that serve every page in ~20
   languages behind a `?hl=` parameter therefore enumerated ~20 near-
   identical URLs per document, and the `max_pages` cap — applied to that
   inflated list — spent the entire budget on translations of the first
   handful of documents. `canonicalize_url` strips those parameters so the
   variants collapse onto one URL and the cap is spent on distinct
   documents. See `NOISE_QUERY_PARAMS` for the deny-list and why it is a
   deny-list rather than an allow-list.

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

import ipaddress
import os
import socket
from urllib.parse import parse_qsl, quote, urlencode, urlparse
from xml.etree import ElementTree  # kept for ElementTree.ParseError below

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


# ---------------------------------------------------------------------------
# SSRF guard
#
# NOTE: mcp-server carries a PARALLEL COPY of `_resolve_is_private` /
# `url_host_is_private` (it cannot import from `ingestion`). Keep the two
# implementations byte-for-byte equivalent in behavior — if you change the
# classification rules or the fail-closed semantics here, change them there
# too, in the same commit.
# ---------------------------------------------------------------------------


def _addr_is_private(addr: str) -> bool:
    """True if a *literal* address string is in a non-routable range."""
    if os.environ.get("SELF_DOCS_ALLOW_PRIVATE_ADDRESSES") == "1":
        return False
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.is_private or ip.is_link_local or ip.is_loopback or ip.is_reserved


class UnresolvableHost(Exception):
    """Raised by `_resolve_host_addrs` when a host cannot be resolved."""


def _resolve_host_addrs(host: str) -> tuple[str, ...]:
    """Resolve `host` to its address literals. Raises `UnresolvableHost`.

    DELIBERATELY UNCACHED. This function previously carried
    `@lru_cache(maxsize=512)`, which was removed as a security fix: an
    `lru_cache` has a size bound but no TIME bound, so it pinned a
    *security decision* for the whole process lifetime while the actual
    fetch (httpx) kept resolving fresh. A host that resolved public once
    would keep being judged public after it was repointed into private
    space — enforcement reading stale data while the action reads fresh
    data, which is exactly backwards.

    The performance argument for caching was weak anyway: this runs once
    per source (at config-validation and at crawl start), plus once per
    off-host child-sitemap candidate — not once per crawled page. A
    400-page crawl triggers a handful of resolutions, not 400, and the OS
    stub resolver already caches at the layer where a TTL is honored.

    `cache_clear` below is a no-op compatibility shim; see its note.
    """
    if not host:
        raise UnresolvableHost(host)
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, ValueError) as e:
        raise UnresolvableHost(host) from e
    if not infos:
        raise UnresolvableHost(host)
    return tuple(info[4][0] for info in infos)


def _resolve_host_addrs_cache_clear() -> None:
    """No-op. `_resolve_host_addrs` is no longer cached (see its docstring),
    but several test fixtures call `_resolve_host_addrs.cache_clear()` to
    keep monkeypatched `socket.getaddrinfo` maps from leaking between
    tests. Keeping the attribute callable means those fixtures stay correct
    (there is now simply nothing to clear) instead of erroring. Do NOT
    reintroduce a cache to give this something to do."""


_resolve_host_addrs.cache_clear = _resolve_host_addrs_cache_clear  # type: ignore[attr-defined]


def _resolve_is_private(host: str) -> bool:
    """True if `host` is, or resolves to, a private/loopback/link-local/
    reserved address.

    Resolution is delegated to the OS resolver, which normalizes the
    decimal/octal/hex integer forms of an IPv4 literal (`2130706433`,
    `0177.0.0.1`) that `ipaddress.ip_address` alone rejects — running
    `ip_address()` over the `getaddrinfo` RESULT therefore closes those
    encodings for free.

    FAILS CLOSED: an unresolvable host returns True. An unresolvable host is
    not fetchable anyway, so treating it as private costs nothing and avoids
    a resolver hiccup silently opening the gate.

    RESIDUAL RISK (consciously accepted): this check resolves the host
    SEPARATELY from the connection that httpx later makes, so the two can
    disagree. A DNS-rebinding attacker who answers with a public address for
    this lookup and a private one for httpx's own lookup is not stopped.

    The size of that window is now bounded by DNS itself: `_resolve_host_addrs`
    is uncached, so every call re-queries and the stale answer lives only as
    long as the OS/upstream resolver honors the record's TTL. It is NOT a
    process-lifetime window — an in-process `lru_cache` here previously made it
    one, which is why that cache was removed.

    Closing the remaining gap entirely requires resolve-then-pin with a custom
    transport or an egress proxy; both were explicitly ruled out. The attacker
    must additionally get a human to click Approve on the source, and the
    crawl-time gate in `crawler.py` re-runs this check (with a fresh
    resolution) against the FINAL post-redirect URL.
    """
    if os.environ.get("SELF_DOCS_ALLOW_PRIVATE_ADDRESSES") == "1":
        return False
    try:
        addrs = _resolve_host_addrs(host)
    except UnresolvableHost:
        return True
    return any(_addr_is_private(a) for a in addrs)


def url_host_is_private(url: str, *, unresolvable_is_private: bool = True) -> bool:
    """True if `url`'s host is, or resolves to, a non-routable address.

    `unresolvable_is_private` selects what an UNRESOLVABLE host means, and
    the two call sites want different answers:

    - Crawl time (default True — fail closed): an unresolvable host is not
      fetchable, so refusing costs nothing.
    - Config-validation time (False): a source must NOT be permanently
      rejected because DNS blipped, or because the host is a placeholder
      that does not resolve from the validating machine. This does not weaken
      the guard — nothing is fetched at validation time, and the fail-closed
      crawl-time gate still refuses the request if the host is unresolvable
      or resolves into private space later.

    A URL with no parseable host is always private (fails closed).
    """
    if os.environ.get("SELF_DOCS_ALLOW_PRIVATE_ADDRESSES") == "1":
        return False
    try:
        host = urlparse(url).hostname
    except ValueError:
        return True
    if not host:
        return True
    try:
        addrs = _resolve_host_addrs(host)
    except UnresolvableHost:
        # An IP literal never reaches the resolver in practice, but if the
        # OS declined to parse it, still classify the literal form itself.
        return unresolvable_is_private or _addr_is_private(host)
    return any(_addr_is_private(a) for a in addrs)


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


# ---------------------------------------------------------------------------
# URL canonicalization
# ---------------------------------------------------------------------------

# Query parameters that select a TRANSLATION or carry ANALYTICS, never
# content. Stripping them collapses the variants onto the canonical URL.
#
# This is deliberately a DENY-list, not an allow-list. Plenty of doc sites
# put genuinely content-bearing state in the query string — `?tech=python`
# picks a language-specific code sample, `?apix_params=...` drives an
# embedded API explorer, `?client_type=...` switches an SDK's reference —
# and those are all live in the current corpus. An allow-list would have to
# enumerate every such parameter across every upstream site and would
# silently drop real documents the moment a site invented a new one. A
# deny-list fails the safe way instead: an unknown parameter is preserved,
# so the worst case is a duplicate we already tolerate today rather than a
# missing page.
#
# Note `hl=` is Google's locale parameter and is the specific one that broke
# the gemini-api and google-search-console-api crawls. `referrer` is on the
# list because developers.google.com stamps it onto in-page links
# (`?referrer=rootllms` in the live corpus).
#
# Entries are held to the same fail-safe standard the deny-list exists for,
# which rules out several "obvious" candidates. `ref` is attribution on most
# sites but selects a git branch/tag on others (`?ref=main`); `source` is
# attribution on Google properties but arbitrary state elsewhere; a bare `l`
# is too generic to attribute to anything. Each of those could collapse two
# genuinely different documents into one — the failure mode this list is
# supposed to avoid — so they are deliberately EXCLUDED. Only add a
# parameter here when its value cannot change the rendered content.
NOISE_QUERY_PARAMS = frozenset(
    {
        # Locale / translation selectors
        "hl",
        "lang",
        "language",
        "locale",
        "setlang",
        # Analytics / attribution
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "gclid",
        "fbclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "referrer",
        "referer",
        "_ga",
        "_gl",
    }
)


def canonicalize_url(url: str) -> str:
    """Strip the fragment and every `NOISE_QUERY_PARAMS` entry from `url`.

    Returns a URL that is equivalent for indexing purposes, so that
    `.../docs/models`, `.../docs/models#pricing`, `.../docs/models?hl=fr`
    and `.../docs/models?hl=ja&utm_source=x` all collapse to the single
    canonical `.../docs/models`.

    Parameter names are matched CASE-INSENSITIVELY (`?HL=fr` is the same
    selector as `?hl=fr`). Content-bearing parameters are preserved, in
    their original order, along with their original encoding of spaces and
    reserved characters — `parse_qsl(keep_blank_values=True)` +
    `urlencode(quote_via=quote)` round-trips a parameter that is kept
    without mangling it, and blank values (`?tech=`) survive as blanks
    rather than being silently dropped.

    A URL with no query and no fragment is returned UNCHANGED (byte-for-
    byte, not re-serialized), so canonicalization can never perturb the
    overwhelming majority of URLs that have nothing to strip. This matters:
    every URL flows through here, and a re-serialization round-trip is a
    chance to alter a path's percent-encoding for no benefit.

    Malformed input that `urlparse` rejects is returned unchanged — this is
    a normalizer, not a validator; the host/scope/private-address gates run
    separately and still refuse it.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return url

    if not parsed.query and not parsed.fragment:
        return url

    if parsed.query:
        kept = [
            (name, value)
            for name, value in parse_qsl(parsed.query, keep_blank_values=True)
            if name.lower() not in NOISE_QUERY_PARAMS
        ]
        query = urlencode(kept, quote_via=quote)
    else:
        query = ""

    return parsed._replace(query=query, fragment="").geturl()


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
        # defusedxml forbids DTDs / entity expansion / external entities by
        # default — sitemap XML is fetched from untrusted upstream sources, so
        # this closes billion-laughs / XXE against the crawler. Both malformed
        # XML and a rejected entity/DTD attack surface as ValueError here.
        root = DefusedET.fromstring(xml_bytes)
    except (ElementTree.ParseError, DefusedXmlException) as e:
        raise ValueError(f"parse_sitemap: malformed or unsafe XML: {e}") from e

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
