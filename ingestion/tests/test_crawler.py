import inspect

import httpx

from app.config import SourceConfig
from app.crawler import RateLimiter, _is_private_ip_host, _validate_final_url, crawl, discover_sitemap_urls
from app.logging_config import get_logger

ROBOTS_ALLOW_ALL = "User-agent: *\nAllow: /\n"
ROBOTS_DISALLOW_PRIVATE = "User-agent: *\nDisallow: /private/\n"

SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/docs/a</loc></url>
  <url><loc>https://example.com/docs/b</loc></url>
  <url><loc>https://example.com/blog/c</loc></url>
</urlset>
"""

PAGE_HTML = "<html><body><h1>Doc Page</h1><p>content</p></body></html>"


def _handler_factory(robots_body=ROBOTS_ALLOW_ALL, sitemap_body=None, page_bodies=None):
    page_bodies = page_bodies or {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=robots_body)
        if sitemap_body is not None and url.endswith("sitemap.xml"):
            return httpx.Response(200, text=sitemap_body)
        if url in page_bodies:
            return httpx.Response(200, text=page_bodies[url])
        return httpx.Response(404, text="not found")

    return handler


def make_client(handler) -> httpx.Client:
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


def test_sitemap_discovery_respects_include_exclude_and_max_pages():
    source = SourceConfig(
        name="example",
        base_url="https://example.com/",
        sitemap="https://example.com/sitemap.xml",
        include_prefixes=["/docs/"],
        exclude_prefixes=["/blog/"],
        max_pages=10,
        rate_limit_rps=1000,
    )
    handler = _handler_factory(
        sitemap_body=SITEMAP_XML,
        page_bodies={
            "https://example.com/docs/a": PAGE_HTML,
            "https://example.com/docs/b": PAGE_HTML,
            "https://example.com/blog/c": PAGE_HTML,
        },
    )
    client = make_client(handler)
    pages = list(crawl(source, client=client))
    urls = {p["url"] for p in pages}
    assert urls == {"https://example.com/docs/a", "https://example.com/docs/b"}


def test_sitemap_discovery_bounded_by_max_pages():
    source = SourceConfig(
        name="example",
        base_url="https://example.com/",
        sitemap="https://example.com/sitemap.xml",
        include_prefixes=["/docs/"],
        max_pages=1,
        rate_limit_rps=1000,
    )
    handler = _handler_factory(
        sitemap_body=SITEMAP_XML,
        page_bodies={
            "https://example.com/docs/a": PAGE_HTML,
            "https://example.com/docs/b": PAGE_HTML,
        },
    )
    client = make_client(handler)
    pages = list(crawl(source, client=client))
    assert len(pages) == 1


def test_bfs_fallback_when_no_sitemap():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        if url == "https://example.com/":
            return httpx.Response(
                200,
                text='<html><body><a href="/docs/a">A</a><a href="https://other.com/x">ext</a></body></html>',
            )
        if url == "https://example.com/docs/a":
            return httpx.Response(200, text=PAGE_HTML)
        return httpx.Response(404)

    source = SourceConfig(
        name="example",
        base_url="https://example.com/",
        max_pages=10,
        rate_limit_rps=1000,
    )
    client = make_client(handler)
    pages = list(crawl(source, client=client))
    urls = {p["url"] for p in pages}
    assert "https://example.com/" in urls
    assert "https://example.com/docs/a" in urls
    assert not any("other.com" in u for u in urls)


def test_cross_host_redirect_is_rejected():
    """A response whose FINAL url (after following redirects) lands on a
    different host than base_url must be rejected, not indexed (security
    review L1)."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        if url == "https://example.com/":
            return httpx.Response(302, headers={"Location": "https://evil.com/"})
        if url == "https://evil.com/":
            return httpx.Response(200, text=PAGE_HTML)
        return httpx.Response(404)

    source = SourceConfig(
        name="example",
        base_url="https://example.com/",
        max_pages=10,
        rate_limit_rps=1000,
    )
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, follow_redirects=True, max_redirects=5)
    pages = list(crawl(source, client=client))
    assert len(pages) == 1
    assert pages[0] == {"url": "https://example.com/", "html": None, "fetch_ok": False}


def test_private_ip_redirect_rejected_when_base_host_public():
    """A redirect to an IP literal in a private/link-local range must be
    rejected when the source's configured host is public (security review
    L1)."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        if url == "https://8.8.8.8/":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})
        if url == "http://169.254.169.254/":
            return httpx.Response(200, text=PAGE_HTML)
        return httpx.Response(404)

    source = SourceConfig(
        name="example",
        base_url="https://8.8.8.8/",
        max_pages=10,
        rate_limit_rps=1000,
    )
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, follow_redirects=True, max_redirects=5)
    pages = list(crawl(source, client=client))
    assert len(pages) == 1
    assert pages[0] == {"url": "https://8.8.8.8/", "html": None, "fetch_ok": False}


def test_is_private_ip_host_detects_rfc1918_and_link_local():
    assert _is_private_ip_host("10.0.0.1") is True
    assert _is_private_ip_host("192.168.1.1") is True
    assert _is_private_ip_host("169.254.169.254") is True  # link-local / cloud metadata
    assert _is_private_ip_host("127.0.0.1") is True  # loopback
    assert _is_private_ip_host("8.8.8.8") is False
    assert _is_private_ip_host("example.com") is False  # hostname, not an IP literal


def test_validate_final_url_rejects_private_ip_literal_when_base_host_public():
    # Same "host" string on both sides so the same-host check alone would
    # pass — the private-IP check is the thing rejecting this.
    assert _validate_final_url(
        final_url="http://169.254.169.254/",
        base_url="http://169.254.169.254/",
        include_prefixes=[],
        exclude_prefixes=[],
        base_host_is_public=True,
    ) is False


def test_validate_final_url_allows_private_ip_when_base_host_is_itself_private():
    assert _validate_final_url(
        final_url="http://169.254.169.254/",
        base_url="http://169.254.169.254/",
        include_prefixes=[],
        exclude_prefixes=[],
        base_host_is_public=False,
    ) is True


def test_robots_disallow_blocks_page():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_DISALLOW_PRIVATE)
        if url == "https://example.com/private/secret":
            return httpx.Response(200, text=PAGE_HTML)
        return httpx.Response(200, text=PAGE_HTML)

    source = SourceConfig(
        name="example",
        base_url="https://example.com/private/secret",
        max_pages=10,
        rate_limit_rps=1000,
    )
    client = make_client(handler)
    pages = list(crawl(source, client=client))
    assert len(pages) == 1
    assert pages[0] == {"url": "https://example.com/private/secret", "html": None, "fetch_ok": False}


def test_page_fetch_non_200_yields_fetch_ok_false():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        if url == "https://example.com/error503":
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, text=PAGE_HTML)

    source = SourceConfig(
        name="example",
        base_url="https://example.com/error503",
        max_pages=10,
        rate_limit_rps=1000,
    )
    client = make_client(handler)
    pages = list(crawl(source, client=client))
    assert len(pages) == 1
    assert pages[0] == {"url": "https://example.com/error503", "html": None, "fetch_ok": False}


def test_page_fetch_exception_yields_fetch_ok_false():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        if url == "https://example.com/timeout":
            raise httpx.ConnectTimeout("Connection timed out")
        return httpx.Response(200, text=PAGE_HTML)

    source = SourceConfig(
        name="example",
        base_url="https://example.com/timeout",
        max_pages=10,
        rate_limit_rps=1000,
    )
    client = make_client(handler)
    pages = list(crawl(source, client=client))
    assert len(pages) == 1
    assert pages[0] == {"url": "https://example.com/timeout", "html": None, "fetch_ok": False}


def test_scope_rejection_yields_nothing():
    """Scope and private-IP rejections are dropped entirely (`None` without
    yielding) so that pages excluded by scope or not on same host remain
    purgeable."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        if url == "https://example.com/":
            return httpx.Response(
                200,
                text='<html><body><a href="https://other.com/x">ext</a><a href="/excluded/path">exc</a></body></html>',
            )
        return httpx.Response(200, text=PAGE_HTML)

    source = SourceConfig(
        name="example",
        base_url="https://example.com/",
        exclude_prefixes=["/excluded/"],
        max_pages=10,
        rate_limit_rps=1000,
    )
    client = make_client(handler)
    pages = list(crawl(source, client=client))
    urls = [p["url"] for p in pages]
    assert urls == ["https://example.com/"]


def test_crawl_is_a_generator():
    """crawl() must stay a generator (yields per page) so store.sync_source
    can commit each page immediately instead of losing an idle DB connection
    across a long crawl (see module docstring / crawl() docstring)."""
    source = SourceConfig(
        name="example",
        base_url="https://example.com/",
        max_pages=10,
        rate_limit_rps=1000,
    )
    client = make_client(_handler_factory())
    result = crawl(source, client=client)
    assert inspect.isgenerator(result)
    result.close()


def test_scope_leak_traefik_hub_rejected_traefik_routing_accepted():
    """Regression test for the traefik scope leak: include_prefixes=["/traefik"]
    must not also match /traefik-hub/..., /traefik-enterprise/...,
    /traefik-mesh/... (different products sharing a string prefix). This
    drives the rejection through crawler's real BFS filtering path, not just
    urlscope's own unit tests."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        if url == "https://example.com/traefik/":
            return httpx.Response(
                200,
                text=(
                    '<html><body>'
                    '<a href="/traefik/routing">routing</a>'
                    '<a href="/traefik-hub/x">hub</a>'
                    '<a href="/traefik-enterprise/y">enterprise</a>'
                    '<a href="/traefik-mesh/z">mesh</a>'
                    '</body></html>'
                ),
            )
        if url in (
            "https://example.com/traefik/routing",
            "https://example.com/traefik-hub/x",
            "https://example.com/traefik-enterprise/y",
            "https://example.com/traefik-mesh/z",
        ):
            return httpx.Response(200, text=PAGE_HTML)
        return httpx.Response(404)

    source = SourceConfig(
        name="example",
        base_url="https://example.com/traefik/",
        include_prefixes=["/traefik"],
        max_pages=10,
        rate_limit_rps=1000,
    )
    client = make_client(handler)
    pages = list(crawl(source, client=client))
    urls = {p["url"] for p in pages}
    assert "https://example.com/traefik/routing" in urls
    assert "https://example.com/traefik-hub/x" not in urls
    assert "https://example.com/traefik-enterprise/y" not in urls
    assert "https://example.com/traefik-mesh/z" not in urls


SITEMAP_INDEX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://appwrite.io/sitemaps/pages.xml</loc></sitemap>
  <sitemap><loc>https://appwrite.io/sitemaps/docs.xml</loc></sitemap>
  <sitemap><loc>https://appwrite.io/sitemaps/broken.xml</loc></sitemap>
</sitemapindex>
"""

CHILD_PAGES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://appwrite.io/</loc></url>
</urlset>
"""

CHILD_DOCS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://appwrite.io/docs/a</loc></url>
  <url><loc>https://appwrite.io/docs/b</loc></url>
</urlset>
"""


def _new_limiter() -> RateLimiter:
    return RateLimiter(1000)


def _test_log():
    return get_logger(component="crawler-test")


class _RecordingLog:
    """Minimal structlog-shaped stub that records `.info(event, **fields)`
    calls so tests can assert on which log events fired without depending
    on stdout capture."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def info(self, event, **kwargs):
        self.events.append((event, kwargs))

    def bind(self, **kwargs):
        return self


def test_sitemap_truncated_at_cap_fires_with_numbers_on_truncation():
    """The cap must emit a distinct `sitemap_truncated_at_cap` event, once
    per discovery (not once per skipped URL), with the numbers an operator
    needs: cap, collected count, and how many extra in-scope candidates were
    seen beyond the cap (computed cheaply from already-fetched data)."""
    sitemap_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<url><loc>https://example.com/docs/{i}</loc></url>" for i in range(10))
        + "</urlset>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://example.com/sitemap.xml":
            return httpx.Response(200, text=sitemap_xml)
        return httpx.Response(404)

    client = make_client(handler)
    log = _RecordingLog()
    urls = discover_sitemap_urls(
        client,
        "https://example.com/sitemap.xml",
        max_pages=3,
        limiter=_new_limiter(),
        log=log,
        base_url="https://example.com/",
        include_prefixes=[],
        exclude_prefixes=[],
    )
    assert len(urls) == 3

    truncation_events = [e for e in log.events if e[0] == "sitemap_truncated_at_cap"]
    assert len(truncation_events) == 1
    _, fields = truncation_events[0]
    assert fields["cap"] == 3
    assert fields["collected"] == 3
    # 10 in-scope URLs total, 3 collected -> 7 extra seen beyond the cap.
    assert fields["extra_seen_in_scope"] == 7


def test_sitemap_truncated_at_cap_does_not_fire_when_under_cap():
    """A crawl that finishes under the cap must emit no truncation event."""
    sitemap_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<url><loc>https://example.com/docs/{i}</loc></url>" for i in range(3))
        + "</urlset>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://example.com/sitemap.xml":
            return httpx.Response(200, text=sitemap_xml)
        return httpx.Response(404)

    client = make_client(handler)
    log = _RecordingLog()
    urls = discover_sitemap_urls(
        client,
        "https://example.com/sitemap.xml",
        max_pages=10,
        limiter=_new_limiter(),
        log=log,
        base_url="https://example.com/",
        include_prefixes=[],
        exclude_prefixes=[],
    )
    assert len(urls) == 3
    assert [e for e in log.events if e[0] == "sitemap_truncated_at_cap"] == []


def test_discover_sitemap_urls_recurses_into_sitemapindex_children():
    """Regression test for appwrite: the root sitemap.xml is a
    <sitemapindex> whose children are separate sitemap.xml files. Discovery
    must recurse one level and return the children's page URLs."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://appwrite.io/sitemap.xml":
            return httpx.Response(200, text=SITEMAP_INDEX_XML)
        if url == "https://appwrite.io/sitemaps/pages.xml":
            return httpx.Response(200, text=CHILD_PAGES_XML)
        if url == "https://appwrite.io/sitemaps/docs.xml":
            return httpx.Response(200, text=CHILD_DOCS_XML)
        if url == "https://appwrite.io/sitemaps/broken.xml":
            return httpx.Response(500, text="server error")
        return httpx.Response(404)

    client = make_client(handler)
    urls = discover_sitemap_urls(
        client,
        "https://appwrite.io/sitemap.xml",
        max_pages=100,
        limiter=_new_limiter(),
        log=_test_log(),
        base_url="https://appwrite.io/",
        include_prefixes=[],
        exclude_prefixes=[],
    )
    assert "https://appwrite.io/" in urls
    assert "https://appwrite.io/docs/a" in urls
    assert "https://appwrite.io/docs/b" in urls


def test_discover_sitemap_urls_failing_child_does_not_abort_discovery():
    """A child sitemap that 500s (or fails to parse) must be logged and
    skipped, not abort discovery of the other children."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://appwrite.io/sitemap.xml":
            return httpx.Response(200, text=SITEMAP_INDEX_XML)
        if url == "https://appwrite.io/sitemaps/pages.xml":
            return httpx.Response(200, text=CHILD_PAGES_XML)
        if url == "https://appwrite.io/sitemaps/docs.xml":
            # malformed XML -> ValueError from parse_sitemap
            return httpx.Response(200, text="<not-a-sitemap>")
        if url == "https://appwrite.io/sitemaps/broken.xml":
            return httpx.Response(500, text="server error")
        return httpx.Response(404)

    client = make_client(handler)
    urls = discover_sitemap_urls(
        client,
        "https://appwrite.io/sitemap.xml",
        max_pages=100,
        limiter=_new_limiter(),
        log=_test_log(),
        base_url="https://appwrite.io/",
        include_prefixes=[],
        exclude_prefixes=[],
    )
    # pages.xml succeeded despite docs.xml and broken.xml both failing.
    assert urls == ["https://appwrite.io/"]


def test_discover_sitemap_urls_bounded_by_max_pages_across_children():
    """max_pages must bound total IN-SCOPE URLs collected across children,
    mixing in-scope and out-of-scope entries (not an all-in-scope sitemap) —
    regression coverage for the truncation-before-filtering bug."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://appwrite.io/sitemap.xml":
            return httpx.Response(200, text=SITEMAP_INDEX_XML)
        if url == "https://appwrite.io/sitemaps/pages.xml":
            return httpx.Response(200, text=CHILD_PAGES_XML)
        if url == "https://appwrite.io/sitemaps/docs.xml":
            return httpx.Response(200, text=CHILD_DOCS_XML)
        if url == "https://appwrite.io/sitemaps/broken.xml":
            return httpx.Response(500, text="server error")
        return httpx.Response(404)

    client = make_client(handler)
    # pages.xml's "https://appwrite.io/" is out of scope under include=/docs;
    # docs.xml's two /docs/* URLs are in scope. max_pages=2 must land exactly
    # on the two in-scope URLs, not truncate before filtering was applied.
    urls = discover_sitemap_urls(
        client,
        "https://appwrite.io/sitemap.xml",
        max_pages=2,
        limiter=_new_limiter(),
        log=_test_log(),
        base_url="https://appwrite.io/",
        include_prefixes=["/docs"],
        exclude_prefixes=[],
    )
    assert len(urls) == 2
    assert set(urls) == {"https://appwrite.io/docs/a", "https://appwrite.io/docs/b"}


def test_discover_sitemap_urls_scope_filter_applied_before_cap():
    """Regression test modeled on the real docs.docker.com/sitemap.xml case:
    a whole-host sitemap whose first N entries are ALL out-of-scope
    (include_prefixes=["/compose"]) and whose in-scope URLs appear only
    later. Filtering by scope must happen BEFORE max_pages truncates the
    list, or discovery hands back an incomplete-but-treated-as-complete
    enumeration and `_delete_missing_pages` purges the real in-scope pages
    that got truncated away."""
    out_of_scope_locs = "".join(f"<url><loc>https://docs.docker.com/other/{i}</loc></url>" for i in range(186))
    in_scope_locs = "".join(f"<url><loc>https://docs.docker.com/compose/{i}</loc></url>" for i in range(14))
    sitemap_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{out_of_scope_locs}{in_scope_locs}"
        "</urlset>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://docs.docker.com/sitemap.xml":
            return httpx.Response(200, text=sitemap_xml)
        return httpx.Response(404)

    client = make_client(handler)
    urls = discover_sitemap_urls(
        client,
        "https://docs.docker.com/sitemap.xml",
        max_pages=200,
        limiter=_new_limiter(),
        log=_test_log(),
        base_url="https://docs.docker.com/",
        include_prefixes=["/compose"],
        exclude_prefixes=[],
    )
    assert len(urls) == 14
    assert all(u.startswith("https://docs.docker.com/compose/") for u in urls)


def test_discover_sitemap_urls_depth_capped_for_sitemap_index_of_indexes():
    """A sitemap index of sitemap indexes must log and stop rather than
    recurse unbounded."""
    nested_index = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemaps/grandchild.xml</loc></sitemap>
</sitemapindex>
"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://example.com/sitemap.xml":
            return httpx.Response(
                200,
                text=(
                    '<?xml version="1.0"?>'
                    '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    '<sitemap><loc>https://example.com/sitemaps/child-index.xml</loc></sitemap>'
                    '</sitemapindex>'
                ),
            )
        if url == "https://example.com/sitemaps/child-index.xml":
            return httpx.Response(200, text=nested_index)
        if url == "https://example.com/sitemaps/grandchild.xml":
            return httpx.Response(200, text=CHILD_DOCS_XML)
        return httpx.Response(404)

    client = make_client(handler)
    urls = discover_sitemap_urls(
        client,
        "https://example.com/sitemap.xml",
        max_pages=100,
        limiter=_new_limiter(),
        log=_test_log(),
        base_url="https://example.com/",
        include_prefixes=[],
        exclude_prefixes=[],
    )
    # depth 0 (root) -> depth 1 (child-index, itself a sitemapindex) hits the
    # cap and returns without recursing into grandchild.xml.
    assert urls == []


def test_discover_sitemap_urls_self_referencing_index_terminates():
    """A self-referencing sitemap index must terminate instead of looping."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://example.com/sitemap.xml":
            return httpx.Response(
                200,
                text=(
                    '<?xml version="1.0"?>'
                    '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    '<sitemap><loc>https://example.com/sitemap.xml</loc></sitemap>'
                    '</sitemapindex>'
                ),
            )
        return httpx.Response(404)

    client = make_client(handler)
    urls = discover_sitemap_urls(
        client,
        "https://example.com/sitemap.xml",
        max_pages=100,
        limiter=_new_limiter(),
        log=_test_log(),
        base_url="https://example.com/",
        include_prefixes=[],
        exclude_prefixes=[],
    )
    assert urls == []


def test_crawl_falls_back_to_bfs_on_sitemap_valueerror():
    """parse_sitemap raises ValueError on malformed/unexpected-root XML;
    crawl()'s except clause must include ValueError so today's graceful
    BFS-fallback behavior (sitemap_failed_fallback_bfs) is preserved. This
    matters live: traefik's configured sitemap currently 404s."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        if url.endswith("sitemap.xml"):
            # unexpected root element -> parse_sitemap raises ValueError
            return httpx.Response(200, text="<not-a-sitemap-or-index/>")
        if url == "https://example.com/":
            return httpx.Response(200, text='<html><body><a href="/docs/a">A</a></body></html>')
        if url == "https://example.com/docs/a":
            return httpx.Response(200, text=PAGE_HTML)
        return httpx.Response(404)

    source = SourceConfig(
        name="example",
        base_url="https://example.com/",
        sitemap="https://example.com/sitemap.xml",
        max_pages=10,
        rate_limit_rps=1000,
    )
    client = make_client(handler)
    pages = list(crawl(source, client=client))
    urls = {p["url"] for p in pages}
    assert "https://example.com/" in urls
    assert "https://example.com/docs/a" in urls


def test_extract_links_suppresses_xml_warning(recwarn):
    """extract_links() should not emit XMLParsedAsHTMLWarning when processing XML content."""
    from bs4 import XMLParsedAsHTMLWarning
    from app.crawler import extract_links

    xml_content = '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.com/page1</loc></url></urlset>'
    extract_links(xml_content, "https://example.com/sitemap.xml")
    assert not [w for w in recwarn if issubclass(w.category, XMLParsedAsHTMLWarning)]

