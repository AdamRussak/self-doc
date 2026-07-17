"""URL discovery + rate-limited fetch for a single doc source.

Discovery order per source: `sitemap.xml` when configured (falls back to BFS
if the sitemap fetch/parse fails), else same-host breadth-first crawl from
`base_url`, bounded by `max_pages`. Honors `robots.txt` via
`urllib.robotparser`, applies a per-source token-bucket-ish rate limit
(`rate_limit_rps`, default 1.0 req/sec), and identifies itself with a custom
User-Agent. `exclude_prefixes` always wins over `include_prefixes`.
"""

from __future__ import annotations

import ipaddress
import time
import urllib.robotparser
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup

from .config import SourceConfig
from .logging_config import get_logger

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
    """exclude_prefixes always wins over include_prefixes."""
    if any(path.startswith(p) for p in exclude_prefixes):
        return False
    if include_prefixes:
        return any(path.startswith(p) for p in include_prefixes)
    return True


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


def discover_sitemap_urls(client: httpx.Client, sitemap_url: str) -> list[str]:
    """Parse a sitemap.xml (with or without the sitemap XML namespace) and
    return the list of <loc> URLs. Raises on network/parse failure so callers
    can fall back to BFS."""
    resp = client.get(sitemap_url, headers={"User-Agent": USER_AGENT}, timeout=15)
    resp.raise_for_status()
    root = ElementTree.fromstring(resp.content)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = [el.text.strip() for el in root.findall(".//sm:loc", ns) if el.text]
    if not urls:
        urls = [el.text.strip() for el in root.findall(".//loc") if el.text]
    return urls


def extract_links(html: str, page_url: str) -> list[str]:
    """Extract absolute, fragment-stripped same-document links from an HTML page."""
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "javascript:", "tel:")):
            continue
        links.append(_strip_fragment(urljoin(page_url, href)))
    return links


def crawl(source: SourceConfig, client: httpx.Client | None = None) -> list[dict]:
    """Discover and fetch up to `source.max_pages` pages for `source`.

    Returns a list of `{"url": str, "html": str}` dicts. Pure w.r.t. side
    effects on `client` — pass a mock/fake httpx.Client in tests to avoid
    real network calls.
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

        pages: list[dict] = []
        visited: set[str] = set()

        candidate_urls: list[str] | None = None
        if source.sitemap:
            try:
                candidate_urls = discover_sitemap_urls(client, str(source.sitemap))
                log.info("sitemap_discovered", count=len(candidate_urls))
            except (httpx.HTTPError, ElementTree.ParseError) as e:
                log.info("sitemap_failed_fallback_bfs", error=str(e))
                candidate_urls = None

        def _visit(url: str) -> httpx.Response | None:
            if not _same_host(url, base_url):
                return None
            if not _allowed(urlparse(url).path, source.include_prefixes, source.exclude_prefixes):
                return None
            if not can_fetch(rp, url):
                log.info("robots_disallowed", url=url)
                return None
            limiter.wait()
            start = time.monotonic()
            try:
                resp = client.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
            except httpx.HTTPError as e:
                log.info("page_fetch_failed", url=url, error=str(e))
                return None
            duration_ms = round((time.monotonic() - start) * 1000, 1)
            if resp.status_code != 200:
                log.info("page_fetch_non_200", url=url, status=resp.status_code, duration_ms=duration_ms)
                return None
            final_url = str(resp.url)
            if not _validate_final_url(
                final_url,
                base_url,
                source.include_prefixes,
                source.exclude_prefixes,
                base_host_is_public,
            ):
                log.info("redirect_rejected", url=url, final_url=final_url)
                return None
            log.info("page_fetched", url=url, duration_ms=duration_ms)
            return resp

        if candidate_urls is not None:
            for url in candidate_urls:
                if len(pages) >= source.max_pages:
                    break
                url = _strip_fragment(url)
                if url in visited:
                    continue
                visited.add(url)
                resp = _visit(url)
                if resp is not None:
                    pages.append({"url": url, "html": resp.text})
        else:
            queue = [base_url]
            while queue and len(pages) < source.max_pages:
                url = _strip_fragment(queue.pop(0))
                if url in visited:
                    continue
                visited.add(url)
                resp = _visit(url)
                if resp is None:
                    continue
                pages.append({"url": url, "html": resp.text})
                for link in extract_links(resp.text, url):
                    if link not in visited and _same_host(link, base_url):
                        queue.append(link)

        log.info("crawl_complete", pages_fetched=len(pages))
        return pages
    finally:
        if own_client:
            client.close()
