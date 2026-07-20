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

from . import llms_txt
from .config import SourceConfig
from .logging_config import get_logger
from .urlscope import parse_sitemap
from .urlscope import path_allowed as _path_allowed
from .urlscope import url_host_is_private

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
    """True if `host` is an IP LITERAL within a private/link-local/loopback/
    reserved range. Returns False for plain hostnames — no DNS resolution.

    This is the cheap literal-only precheck; the authoritative check is
    `urlscope.url_host_is_private`, which resolves and therefore also catches
    a public hostname with a private A record and the decimal/octal integer
    encodings of an IPv4 literal."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_link_local or ip.is_loopback or ip.is_reserved


def _validate_final_url(
    final_url: str,
    base_url: str,
    include_prefixes: list[str],
    exclude_prefixes: list[str],
) -> bool:
    """Validate that a candidate url — a response's final url, or a redirect
    target BEFORE the hop is issued — is same-host, allowed by include/
    exclude, and not in private address space.

    The private-address check is UNCONDITIONAL (security review H2). It used
    to be gated on the source's own host being public, which meant a private
    `base_url` did not fail validation, it DISABLED the only private-address
    check in the codebase — a private literal was treated as a licence to
    skip checking."""
    if not _same_host(final_url, base_url):
        return False
    if not _allowed(urlparse(final_url).path, include_prefixes, exclude_prefixes):
        return False
    if url_host_is_private(final_url):
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
            # SSRF guard (security review H1): a <sitemapindex> is fetched
            # content, so its children are attacker-influenced. Check the
            # child's host BEFORE requesting it — the <loc> filtering below
            # happens strictly after the request would have been sent.
            if not _same_host(child_url, base_url) or url_host_is_private(child_url):
                log.info("child_sitemap_out_of_scope", sitemap_url=child_url)
                continue
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


def _looks_like_llms_txt(text: str) -> bool:
    """Cheap sanity gate on a body `llms_txt.discover()` claims came from
    `/llms-full.txt` or `/llms.txt`: `discover()` itself only checks status
    200 / non-empty / size (by design — it is a generic best-effort HTTP
    fetch, not a content-type validator), so a site that 200s an arbitrary
    HTML fallback page for any unknown path (a common pattern) would
    otherwise be treated as a valid llms.txt export.

    The llmstxt.org convention requires the file to open with an H1 markdown
    heading (`# Title`); this checks exactly that on the first non-blank
    line, which is enough to reject an HTML (or otherwise non-llms.txt)
    response without depending on anything `llms_txt.py` doesn't already
    expose."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return stripped.startswith("# ") or stripped == "#"
    return False


def _llms_index_conditional_check(
    client: httpx.Client,
    origin: str,
    conditional: dict[str, tuple[str | None, str | None]],
    log,
) -> tuple[str, str] | tuple[str, str, str] | None:
    """Check the llms-index candidate URLs (`{origin}/llms-full.txt` then
    `{origin}/llms.txt`, same order/preference as `llms_txt.discover`)
    against `conditional` validators, issuing a single conditional GET for
    the first candidate that has a non-None etag/last-modified recorded.

    Returns `("not_modified", url)` on a 304, `("fetched", url, text)` on a
    200 (so the caller can reuse the body instead of double-fetching), or
    `None` if no candidate has a conditional entry, or on any error (falls
    back to a normal, unconditional `llms_txt.discover` call).
    """
    candidates = [f"{origin}/llms-full.txt", f"{origin}/llms.txt"]
    for url in candidates:
        if url not in conditional:
            continue
        etag, last_modified = conditional[url]
        if etag is None and last_modified is None:
            continue
        headers = {"User-Agent": USER_AGENT}
        if etag is not None:
            headers["If-None-Match"] = etag
        if last_modified is not None:
            headers["If-Modified-Since"] = last_modified
        try:
            resp = client.get(url, headers=headers, timeout=15)
        except Exception as e:  # noqa: BLE001 - fall back to normal discover() on any error
            log.info("llms_index_conditional_check_failed", url=url, error=str(e))
            return None
        if resp.status_code == 304:
            return ("not_modified", url)
        if resp.status_code == 200:
            try:
                text = resp.text
            except Exception:  # noqa: BLE001
                return None
            if text.strip():
                return ("fetched", url, text)
        return None
    return None


def crawl(
    source: SourceConfig,
    client: httpx.Client | None = None,
    conditional: dict[str, tuple[str | None, str | None]] | None = None,
) -> Iterator[dict]:
    """Discover and fetch up to `source.max_pages` pages for `source`.

    YIELDS `{"url": str, "html": str | None, "fetch_ok": bool}` dicts one at a
    time as pages are visited, rather than materializing the whole crawl in
    memory first. `fetch_ok=True` (with `html` populated) is yielded on
    successful fetch. `fetch_ok=False` (with `html=None`) is yielded when an
    attempted fetch fails (e.g., `robots_disallowed`, `page_fetch_failed`,
    `page_fetch_non_200`, `redirect_rejected`), enabling downstream callers
    (`store.sync_source`) to add the URL to `seen_urls` and prevent accidental
    purging of existing rows during transient network outages. Scope and
    private-IP rejections are dropped without being yielded. A successful
    200 additionally carries `etag`/`last_modified` keys (only when present
    in the response headers).

    `conditional` maps `url -> (etag, last_modified)` validators from a prior
    sync. When the *first* request for a URL (not a redirect hop) honors a
    304, the item `{"url": url, "not_modified": True, "fetch_ok": True}` is
    yielded instead of re-fetching/re-extracting.

    When `source.llms_txt` is `"auto"` or `"only"`, `crawl` first tries the
    llmstxt.org convention (`llms-full.txt`/`llms.txt`) via `llms_txt`. If a
    file is discovered, its sections are yielded as
    `{"url", "markdown", "heading_path", "fetch_ok": True}` items (capped at
    `source.max_pages`) and the generator returns — the sitemap/BFS HTML
    crawl is skipped entirely. If no file is found: `"auto"` falls through to
    the normal HTML crawl; `"only"` yields nothing. If `conditional` carries a
    validator for the llms-index URL and it 304s, a single sentinel
    `{"kind": "llms_index_unchanged", "url", "not_modified": True,
    "fetch_ok": True}` is yielded and the generator returns.

    Pure w.r.t. side effects on `client` — pass a mock/fake httpx.Client in
    tests to avoid real network calls.
    """
    own_client = client is None
    if own_client:
        # follow_redirects=False: hops are walked MANUALLY in `_visit` so each
        # Location is validated BEFORE its request is issued (security review
        # M1). Delegating to httpx meant every hop was actually fetched and
        # only the final url was inspected — blind SSRF with up to
        # MAX_REDIRECTS hops.
        client = httpx.Client(follow_redirects=False)

    log = logger.bind(source=source.name)
    try:
        base_url = str(source.base_url)
        # UNCONDITIONAL private-address gate (security review H2). Checked
        # once per source, before the robots.txt fetch — which is itself a
        # request to base_url's origin and was previously unguarded.
        if url_host_is_private(base_url):
            log.info("crawl_refused_private_host", base_url=base_url)
            return
        rp = load_robots(client, base_url)
        limiter = RateLimiter(source.rate_limit_rps)

        pages_fetched = 0
        visited: set[str] = set()

        if source.llms_txt in ("auto", "only"):
            parsed_base = urlparse(base_url)
            origin = f"{parsed_base.scheme}://{parsed_base.netloc}"

            index_check = None
            if conditional:
                try:
                    index_check = _llms_index_conditional_check(client, origin, conditional, log)
                except Exception as e:  # noqa: BLE001 - fall back to discover() on any error
                    log.info("llms_index_conditional_check_error", error=str(e))
                    index_check = None

            if index_check is not None and index_check[0] == "not_modified":
                _, unchanged_url = index_check
                log.info("llms_index_not_modified", url=unchanged_url)
                yield {
                    "kind": "llms_index_unchanged",
                    "url": unchanged_url,
                    "not_modified": True,
                    "fetch_ok": True,
                }
                return

            if index_check is not None and index_check[0] == "fetched":
                _, fetched_url, fetched_text = index_check
                discovered = (fetched_url, fetched_text)
            else:
                discovered = llms_txt.discover(client, base_url)

            if discovered is not None and not _looks_like_llms_txt(discovered[1]):
                log.info("llms_txt_content_sanity_check_failed", url=discovered[0])
                discovered = None

            if discovered is not None:
                index_url, llms_text = discovered
                limiter.wait()
                sections = llms_txt.split_llms_full(llms_text, index_url)
                llms_count = 0
                for section in sections:
                    if llms_count >= source.max_pages:
                        break
                    yield {
                        "url": section["url"],
                        "markdown": section["markdown"],
                        "heading_path": section["heading_path"],
                        "fetch_ok": True,
                    }
                    llms_count += 1
                log.info("crawl_complete", pages_fetched=llms_count, mode="llms_txt")
                return
            if source.llms_txt == "only":
                log.info("crawl_complete", pages_fetched=0, mode="llms_txt")
                return
            # source.llms_txt == "auto" and nothing discovered: fall through
            # to the normal sitemap/BFS HTML crawl below.

        candidate_urls: list[str] | None = None
        if source.sitemap and not _same_host(str(source.sitemap), base_url):
            # Defence in depth: SourceConfig already rejects an off-host
            # sitemap (H1). This catches a SourceConfig built via
            # `model_construct` or mutated after validation.
            log.info("sitemap_off_host_ignored", sitemap=str(source.sitemap))
        elif source.sitemap:
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
            start = time.monotonic()
            # Manual redirect walk (security review M1): every hop's Location
            # is validated BEFORE the next request is issued, and
            # `follow_redirects=False` is passed PER REQUEST so a caller-
            # supplied client that defaults to following cannot re-open the
            # hole. Bounded by MAX_REDIRECTS, now explicit rather than
            # delegated to httpx.
            current_url = url
            resp = None
            for _hop in range(MAX_REDIRECTS + 1):
                limiter.wait()
                # Conditional (If-None-Match / If-Modified-Since) headers are
                # only ever sent on the FIRST request for `url` — never on a
                # redirect hop's target, which is a different resource.
                req_headers = {"User-Agent": USER_AGENT}
                if _hop == 0 and conditional and url in conditional:
                    cond_etag, cond_last_modified = conditional[url]
                    if cond_etag is not None:
                        req_headers["If-None-Match"] = cond_etag
                    if cond_last_modified is not None:
                        req_headers["If-Modified-Since"] = cond_last_modified
                try:
                    resp = client.get(
                        current_url,
                        headers=req_headers,
                        timeout=15,
                        follow_redirects=False,
                    )
                except httpx.HTTPError as e:
                    log.info("page_fetch_failed", url=url, error=str(e))
                    return {"url": url, "html": None, "fetch_ok": False}
                if _hop == 0 and resp.status_code == 304:
                    log.info("page_not_modified", url=url)
                    return {"url": url, "not_modified": True, "fetch_ok": True}
                if not resp.is_redirect:
                    break
                location = resp.headers.get("location", "").strip()
                if not location:
                    log.info("redirect_rejected", url=url, final_url=current_url, reason="no_location")
                    return {"url": url, "html": None, "fetch_ok": False}
                next_url = _strip_fragment(urljoin(current_url, location))
                if not _validate_final_url(
                    next_url,
                    base_url,
                    source.include_prefixes,
                    source.exclude_prefixes,
                ):
                    # Refused BEFORE issuing the hop, so the request to a
                    # private/off-host target is never sent at all.
                    log.info("redirect_rejected", url=url, final_url=next_url)
                    return {"url": url, "html": None, "fetch_ok": False}
                current_url = next_url
            else:
                log.info("redirect_rejected", url=url, final_url=current_url, reason="too_many_redirects")
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
            ):
                log.info("redirect_rejected", url=url, final_url=final_url)
                return {"url": url, "html": None, "fetch_ok": False}
            log.info("page_fetched", url=url, duration_ms=duration_ms)
            item = {"url": url, "html": resp.text, "fetch_ok": True}
            resp_etag = resp.headers.get("etag")
            resp_last_modified = resp.headers.get("last-modified")
            if resp_etag is not None:
                item["etag"] = resp_etag
            if resp_last_modified is not None:
                item["last_modified"] = resp_last_modified
            return item

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
