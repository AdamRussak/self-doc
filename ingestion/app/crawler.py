"""URL discovery + rate-limited fetch for a single doc source.

Discovery order per source: `sitemap.xml` when configured (falls back to BFS
if the sitemap fetch/parse fails), else same-host breadth-first crawl from
`base_url`, bounded by `max_pages`. Honors `robots.txt` via
`urllib.robotparser`, applies a per-source token-bucket-ish rate limit
(`rate_limit_rps`, default 1.0 req/sec), and identifies itself with a custom
User-Agent. `exclude_prefixes` always wins over `include_prefixes`.

Yields explicit `fetch_ok=True` / `fetch_ok=False` items for attempted
fetches so downstream (`store.sync_source`) can protect never-started or
transiently failing pages from being purged.
"""

from __future__ import annotations

import ipaddress
import time
import urllib.robotparser
import warnings
from collections.abc import Iterator
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from .config import SourceConfig
from .logging_config import get_logger
from .urlscope import parse_sitemap
from .urlscope import path_allowed as _path_allowed

USER_AGENT = "self-docs-crawler/0.1"

# Redirect chains are bounded: an unbounded chain could be abused to exhaust
# resources or bounce the crawler indefinitely (security review L1).
MAX_REDIRECTS = 5

logger = get_logger(component="crawler")


class RateLimiter:
    """Simple fixed-interval rate limiter: blocks `wait()` until at least
    `1/rps` seconds have elapsed since the previous call."""

    def __init__(self, rps: float):
        self.min_interval = 1.0 / rps if rps and rps > 0 else 0.0
        self._last: float | None = None

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        now = time.monotonic()
        if self._last is not None:
            elapsed = now - self._last
            remaining = self.min_interval - elapsed
            if remaining > 0:
                time.sleep(remaining)
        self._last = time.monotonic()


def _same_host(url: str, base_url: str) -> bool:
    return urlparse(url).netloc == urlparse(base_url).netloc


def _allowed(path: str, include_prefixes: list[str], exclude_prefixes: list[str]) -> bool:
    """exclude_prefixes always wins over include_prefixes.

    Delegates to `urlscope.path_allowed` for path-SEGMENT-BOUNDARY matching
    (a plain `str.startswith` here would let `include_prefixes=["/traefik"]`
    also match `/traefik-hub/...`, `/traefik-enterprise/...`,
    `/traefik-mesh/...` — see urlscope module docstring)."""
    return _path_allowed(path, include_prefixes, exclude_prefixes)


def _is_private_ip_host(host: str) -> bool:
    """True if `host` is an IP literal within a private/link-local/loopback
    range. Returns False for plain hostnames — no DNS resolution is performed
    here (out of scope for this check; see security review L1)."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_link_local or ip.is_loopback


def _validate_final_url(
    final_url: str,
    base_url: str,
    include_prefixes: list[str],
    exclude_prefixes: list[str],
    base_host_is_public: bool,
) -> bool:
    """Re-validate a response's FINAL url (after any redirects) is still
    same-host, allowed by include/exclude, and — when the source's configured
    host is public — not an IP literal in a private/link-local range.
    Guards against a redirect bouncing the crawler off-host (security review
    L1)."""
    if not _same_host(final_url, base_url):
        return False
    if not _allowed(urlparse(final_url).path, include_prefixes, exclude_prefixes):
        return False
    if base_host_is_public and _is_private_ip_host(urlparse(final_url).hostname or ""):
        return False
    return True


def _strip_fragment(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(fragment="").geturl()


def load_robots(client: httpx.Client, base_url: str) -> urllib.robotparser.RobotFileParser:
    """Fetch and parse robots.txt for base_url's origin. Missing/unreachable
    robots.txt is treated as "allow all" (standard convention)."""
    rp = urllib.robotparser.RobotFileParser()
    robots_url = urljoin(base_url, "/robots.txt")
    try:
        resp = client.get(robots_url, headers={"User-Agent": USER_AGENT}, timeout=10)
        rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
    except httpx.HTTPError:
        rp.parse([])
    return rp


def can_fetch(rp: urllib.robotparser.RobotFileParser, url: str) -> bool:
    try:
        return rp.can_fetch(USER_AGENT, url)
    except Exception:  # noqa: BLE001 - robots parsing is best-effort
        return True


# A sitemap index of sitemap indexes should log and stop, not recurse
# unbounded (appwrite-style discovery must be bounded and safe against a
# self-referencing sitemap index).
MAX_SITEMAP_RECURSION_DEPTH = 1


def discover_sitemap_urls(
    client: httpx.Client,
    sitemap_url: str,
    max_pages: int,
    limiter: RateLimiter,
    log,
    base_url: str,
    include_prefixes: list[str],
    exclude_prefixes: list[str],
) -> list[str]:
    """Discover page URLs from a sitemap.xml, recursing into child sitemaps
    when the root is a `<sitemapindex>` (e.g. appwrite.io/sitemap.xml, whose
    children are pages.xml, docs.xml, etc.).

    Candidate `<loc>` entries are filtered by same-host + `include_prefixes`/
    `exclude_prefixes` (via `urlscope.path_allowed`) BEFORE `max_pages` is
    applied. This is load-bearing: a shared, whole-host sitemap (e.g.
    doc.traefik.io/sitemap.xml covering traefik + enterprise + hub + mesh, or
    docs.docker.com/sitemap.xml covering the entire Docker docs site) may put
    only a small minority of its entries in scope for this source. Capping on
    the RAW, unfiltered `<loc>` list would truncate to the first `max_pages`
    URLs regardless of scope and hand back an incomplete-but-treated-as-
    complete enumeration; `store.sync_source`'s `_delete_missing_pages` then
    deletes every real in-scope page absent from that truncated list — i.e.
    it would destroy a live corpus on a *successful* sync. Filtering here,
    before the cap, is what keeps discovery a complete in-scope enumeration.

    Raises on the TOP-LEVEL fetch/parse failure so callers can fall back to
    BFS. A CHILD sitemap that fails to fetch/parse is logged and skipped —
    it does not abort discovery of the other children. Recursion is capped
    at `MAX_SITEMAP_RECURSION_DEPTH` and guarded against cycles (a
    self-referencing sitemap index terminates). Stops early once `max_pages`
    IN-SCOPE URLs have been collected so a huge sitemap cannot blow the page
    budget. Uses `limiter` so recursion into child sitemaps still respects
    the source's configured rate limit.

    When `max_pages` actually truncates discovery (upstream has more in-scope
    URLs than the budget allows), emits a single `sitemap_truncated_at_cap`
    log event — once per call, not once per skipped URL — so an operator
    watching a sync can tell that `max_pages`, not upstream, decided the
    corpus size (traefik currently sits at 397 pages against `max_pages:
    400`; this fires for real the moment upstream crosses 400).
    """
    truncation = {"truncated": False, "extra_seen_in_scope": 0, "unprocessed_child_sitemaps": 0}
    collected = _discover_sitemap_urls_recursive(
        client,
        sitemap_url,
        max_pages,
        limiter,
        log,
        base_url,
        include_prefixes,
        exclude_prefixes,
        depth=0,
        seen_sitemaps=set(),
        truncation=truncation,
    )
    if truncation["truncated"]:
        log.info(
            "sitemap_truncated_at_cap",
            cap=max_pages,
            collected=len(collected),
            extra_seen_in_scope=truncation["extra_seen_in_scope"],
            unprocessed_child_sitemaps=truncation["unprocessed_child_sitemaps"],
        )
    return collected


def _discover_sitemap_urls_recursive(
    client: httpx.Client,
    sitemap_url: str,
    max_pages: int,
    limiter: RateLimiter,
    log,
    base_url: str,
    include_prefixes: list[str],
    exclude_prefixes: list[str],
    *,
    depth: int,
    seen_sitemaps: set[str],
    truncation: dict,
) -> list[str]:
    """Recursive worker for `discover_sitemap_urls`. `truncation` is a shared
    mutable dict accumulated across the whole recursion tree; the public
    wrapper logs from it exactly once after recursion completes."""
    if sitemap_url in seen_sitemaps or max_pages <= 0:
        return []
    seen_sitemaps.add(sitemap_url)

    limiter.wait()
    resp = client.get(sitemap_url, headers={"User-Agent": USER_AGENT}, timeout=15)
    resp.raise_for_status()
    urls, child_sitemaps = parse_sitemap(resp.content)

    filtered: list[str] = []
    for u in urls:
        if not _same_host(u, base_url):
            continue
        if not _allowed(urlparse(u).path, include_prefixes, exclude_prefixes):
            continue
        if u not in filtered:
            filtered.append(u)

    if len(filtered) > max_pages:
        # Overflow computed cheaply from data already fetched — no extra
        # network calls — so operators can tell "truncated by 3" from
        # "truncated by 3000".
        truncation["truncated"] = True
        truncation["extra_seen_in_scope"] += len(filtered) - max_pages
        return filtered[:max_pages]

    collected = filtered

    if child_sitemaps:
        if depth >= MAX_SITEMAP_RECURSION_DEPTH:
            log.info(
                "sitemap_index_depth_capped",
                sitemap_url=sitemap_url,
                children=len(child_sitemaps),
            )
            return collected
        if len(collected) >= max_pages:
            # Budget already exhausted by this level's own in-scope URLs;
            # the child sitemaps are never fetched/explored.
            truncation["truncated"] = True
            truncation["unprocessed_child_sitemaps"] += len(child_sitemaps)
            return collected
        for i, child_url in enumerate(child_sitemaps):
            if len(collected) >= max_pages:
                truncation["truncated"] = True
                truncation["unprocessed_child_sitemaps"] += len(child_sitemaps) - i
                break
            try:
                child_urls = _discover_sitemap_urls_recursive(
                    client,
                    child_url,
                    max_pages - len(collected),
                    limiter,
                    log,
                    base_url,
                    include_prefixes,
                    exclude_prefixes,
                    depth=depth + 1,
                    seen_sitemaps=seen_sitemaps,
                    truncation=truncation,
                )
            except (httpx.HTTPError, ValueError, ElementTree.ParseError) as e:
                log.info("child_sitemap_failed", sitemap_url=child_url, error=str(e))
                continue
            for u in child_urls:
                if u not in collected:
                    collected.append(u)
                if len(collected) >= max_pages:
                    break

    return collected


def extract_links(html: str, page_url: str) -> list[str]:
    """Extract absolute, fragment-stripped same-document links from an HTML page."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "javascript:", "tel:")):
            continue
        links.append(_strip_fragment(urljoin(page_url, href)))
    return links


def crawl(source: SourceConfig, client: httpx.Client | None = None) -> Iterator[dict]:
    """Discover and fetch up to `source.max_pages` pages for `source`.

    YIELDS `{"url": str, "html": str | None, "fetch_ok": bool}` dicts one at a
    time as pages are visited, rather than materializing the whole crawl in
    memory first. `fetch_ok=True` (with `html` populated) is yielded on
    successful fetch. `fetch_ok=False` (with `html=None`) is yielded when an
    attempted fetch fails (e.g., `robots_disallowed`, `page_fetch_failed`,
    `page_fetch_non_200`, `redirect_rejected`), enabling downstream callers
    (`store.sync_source`) to add the URL to `seen_urls` and prevent accidental
    purging of existing rows during transient network outages. Scope and
    private-IP rejections are dropped without being yielded.

    Pure w.r.t. side effects on `client` — pass a mock/fake httpx.Client in
    tests to avoid real network calls.
    """
    own_client = client is None
    if own_client:
        client = httpx.Client(follow_redirects=True, max_redirects=MAX_REDIRECTS)

    log = logger.bind(source=source.name)
    try:
        base_url = str(source.base_url)
        base_host_is_public = not _is_private_ip_host(urlparse(base_url).hostname or "")
        rp = load_robots(client, base_url)
        limiter = RateLimiter(source.rate_limit_rps)

        pages_fetched = 0
        visited: set[str] = set()

        candidate_urls: list[str] | None = None
        if source.sitemap:
            try:
                candidate_urls = discover_sitemap_urls(
                    client,
                    str(source.sitemap),
                    source.max_pages,
                    limiter,
                    log,
                    base_url,
                    source.include_prefixes,
                    source.exclude_prefixes,
                )
                log.info("sitemap_discovered", count=len(candidate_urls))
            except (httpx.HTTPError, ElementTree.ParseError, ValueError) as e:
                log.info("sitemap_failed_fallback_bfs", error=str(e))
                candidate_urls = None

        def _visit(url: str) -> dict | None:
            if not _same_host(url, base_url):
                return None
            if not _allowed(urlparse(url).path, source.include_prefixes, source.exclude_prefixes):
                return None
            if not can_fetch(rp, url):
                log.info("robots_disallowed", url=url)
                return {"url": url, "html": None, "fetch_ok": False}
            limiter.wait()
            start = time.monotonic()
            try:
                resp = client.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
            except httpx.HTTPError as e:
                log.info("page_fetch_failed", url=url, error=str(e))
                return {"url": url, "html": None, "fetch_ok": False}
            duration_ms = round((time.monotonic() - start) * 1000, 1)
            if resp.status_code != 200:
                log.info("page_fetch_non_200", url=url, status=resp.status_code, duration_ms=duration_ms)
                return {"url": url, "html": None, "fetch_ok": False}
            final_url = str(resp.url)
            if not _validate_final_url(
                final_url,
                base_url,
                source.include_prefixes,
                source.exclude_prefixes,
                base_host_is_public,
            ):
                log.info("redirect_rejected", url=url, final_url=final_url)
                return {"url": url, "html": None, "fetch_ok": False}
            log.info("page_fetched", url=url, duration_ms=duration_ms)
            return {"url": url, "html": resp.text, "fetch_ok": True}

        if candidate_urls is not None:
            for url in candidate_urls:
                if pages_fetched >= source.max_pages:
                    break
                url = _strip_fragment(url)
                if url in visited:
                    continue
                visited.add(url)
                item = _visit(url)
                if item is not None:
                    if item.get("fetch_ok", True):
                        pages_fetched += 1
                    yield item
        else:
            queue = [base_url]
            while queue and pages_fetched < source.max_pages:
                url = _strip_fragment(queue.pop(0))
                if url in visited:
                    continue
                visited.add(url)
                item = _visit(url)
                if item is None:
                    continue
                if item.get("fetch_ok", True):
                    pages_fetched += 1
                yield item
                if item.get("fetch_ok", True) and item.get("html"):
                    for link in extract_links(item["html"], url):
                        if link not in visited and _same_host(link, base_url):
                            queue.append(link)

        log.info("crawl_complete", pages_fetched=pages_fetched)
    finally:
        if own_client:
            client.close()
