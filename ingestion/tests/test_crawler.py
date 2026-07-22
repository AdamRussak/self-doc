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


def test_sitemap_discovery_unlimited_when_max_pages_none():
    # max_pages=None (omitted) => no page limit: all in-scope pages are crawled.
    source = SourceConfig(
        name="example",
        base_url="https://example.com/",
        sitemap="https://example.com/sitemap.xml",
        include_prefixes=["/docs/"],
        rate_limit_rps=1000,
    )
    assert source.max_pages is None
    handler = _handler_factory(
        sitemap_body=SITEMAP_XML,
        page_bodies={
            "https://example.com/docs/a": PAGE_HTML,
            "https://example.com/docs/b": PAGE_HTML,
        },
    )
    client = make_client(handler)
    pages = list(crawl(source, client=client))
    assert {p["url"] for p in pages} == {
        "https://example.com/docs/a",
        "https://example.com/docs/b",
    }


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


def test_validate_final_url_rejects_private_ip_literal():
    # Same "host" string on both sides so the same-host check alone would
    # pass — the private-IP check is the thing rejecting this.
    assert _validate_final_url(
        final_url="http://169.254.169.254/",
        base_url="http://169.254.169.254/",
        include_prefixes=[],
        exclude_prefixes=[],
    ) is False


def test_validate_final_url_private_check_is_unconditional(monkeypatch):
    """Security review H2: the private-address check used to be gated on the
    source's own host being public, which meant a private base_url did not
    fail the check — it DISABLED it. A private base_url is now no licence to
    reach further into private space."""
    assert _validate_final_url(
        final_url="http://169.254.169.254/",
        base_url="http://169.254.169.254/",
        include_prefixes=[],
        exclude_prefixes=[],
    ) is False
    assert _validate_final_url(
        final_url="http://192.168.1.10/docs",
        base_url="http://192.168.1.10/",
        include_prefixes=[],
        exclude_prefixes=[],
    ) is False


def test_crawl_refuses_private_base_url_unconditionally():
    """H2: `base_url = http://192.168.1.10/` must be REFUSED, and must not
    even issue the robots.txt request. Built via `model_construct` because
    SourceConfig itself now rejects this at validation time."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=PAGE_HTML)

    source = SourceConfig.model_construct(
        name="internal",
        base_url="http://192.168.1.10/",
        sitemap=None,
        include_prefixes=[],
        exclude_prefixes=[],
        max_pages=10,
        language="english",
        rate_limit_rps=1000,
    )
    client = make_client(handler)
    pages = list(crawl(source, client=client))
    assert pages == []
    assert requested == []  # not even robots.txt was fetched


def test_redirect_into_private_space_refused_before_request_is_issued():
    """M1: httpx used to follow every hop internally and only the FINAL url
    was inspected — the request to private space was actually issued, just
    discarded. The hop must now never be sent."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        if url == "https://example.com/":
            return httpx.Response(302, headers={"Location": "http://10.0.0.5:8080/reboot"})
        return httpx.Response(200, text=PAGE_HTML)

    source = SourceConfig(
        name="example",
        base_url="https://example.com/",
        max_pages=10,
        rate_limit_rps=1000,
    )
    # Deliberately a follow-redirects client: `_visit` passes
    # follow_redirects=False per request, so a caller-supplied client cannot
    # re-open the hole.
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, follow_redirects=True, max_redirects=5)
    pages = list(crawl(source, client=client))

    assert pages == [{"url": "https://example.com/", "html": None, "fetch_ok": False}]
    assert not any("10.0.0.5" in u for u in requested)


def test_same_host_in_scope_redirect_is_still_followed():
    """The manual redirect walk must not break legitimate redirects."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        if url == "https://example.com/docs/old":
            return httpx.Response(301, headers={"Location": "/docs/new"})
        if url == "https://example.com/docs/new":
            return httpx.Response(200, text=PAGE_HTML)
        return httpx.Response(404)

    source = SourceConfig(
        name="example",
        base_url="https://example.com/docs/old",
        max_pages=10,
        rate_limit_rps=1000,
    )
    client = make_client(handler)
    pages = list(crawl(source, client=client))
    assert pages[0]["url"] == "https://example.com/docs/old"
    assert pages[0]["fetch_ok"] is True
    assert pages[0]["html"] == PAGE_HTML


def test_redirect_loop_beyond_max_redirects_yields_fetch_ok_false():
    """A hop budget is enforced explicitly, and exhausting it preserves the
    fetch_ok=False contract (it must NOT silently drop the URL)."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        n = int(url.rsplit("/", 1)[-1] or 0)
        return httpx.Response(302, headers={"Location": f"/hop/{n + 1}"})

    source = SourceConfig(
        name="example",
        base_url="https://example.com/hop/0",
        max_pages=10,
        rate_limit_rps=1000,
    )
    client = make_client(handler)
    pages = list(crawl(source, client=client))
    assert pages == [{"url": "https://example.com/hop/0", "html": None, "fetch_ok": False}]


def test_redirect_without_location_header_yields_fetch_ok_false():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        return httpx.Response(302)

    source = SourceConfig(
        name="example",
        base_url="https://example.com/",
        max_pages=10,
        rate_limit_rps=1000,
    )
    client = make_client(handler)
    pages = list(crawl(source, client=client))
    assert pages == [{"url": "https://example.com/", "html": None, "fetch_ok": False}]


def test_off_host_child_sitemap_is_never_fetched():
    """H1 fan-out: children of a <sitemapindex> are attacker-influenced
    content and must be host-checked BEFORE the child request is issued."""
    requested: list[str] = []
    index_xml = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/child-sitemap.xml</loc></sitemap>
  <sitemap><loc>http://169.254.169.254/latest/meta-data/sitemap.xml</loc></sitemap>
</sitemapindex>
"""
    child_xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/docs/a</loc></url>
</urlset>
"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        if url == "https://example.com/sitemap.xml":
            return httpx.Response(200, text=index_xml)
        if url == "https://example.com/child-sitemap.xml":
            return httpx.Response(200, text=child_xml)
        return httpx.Response(200, text=PAGE_HTML)

    source = SourceConfig(
        name="example",
        base_url="https://example.com/",
        sitemap="https://example.com/sitemap.xml",
        max_pages=10,
        rate_limit_rps=1000,
    )
    client = make_client(handler)
    pages = list(crawl(source, client=client))
    assert [p["url"] for p in pages] == ["https://example.com/docs/a"]
    assert not any("169.254.169.254" in u for u in requested)


# --- llms.txt INDEX vs full-content routing -----------------------------

LLMS_INDEX = """# Example Docs

> Summary of the project.

## Guides
- [Quickstart](https://example.com/docs/quickstart): get started
- [Configuration](https://example.com/docs/config)
- [API](https://example.com/docs/api)
"""

LLMS_FULL = """# Quickstart

Real documentation prose describing how to install and use the package across
several sentences of genuine content that is clearly not a list of links.

## Configuration

More prose about configuration options, again long enough to read as content.
"""


def test_llms_index_is_crawled_as_html_not_ingested_as_content():
    """Regression: a site serving `/llms.txt` (a link INDEX) but no
    `/llms-full.txt` must have its linked pages FETCHED as HTML, not have the
    index link-list ingested verbatim as content."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        if url.endswith("/llms-full.txt"):
            return httpx.Response(404, text="not found")
        if url.endswith("/llms.txt"):
            return httpx.Response(200, text=LLMS_INDEX)
        return httpx.Response(200, text=PAGE_HTML)

    source = SourceConfig(
        name="example",
        base_url="https://example.com/",
        max_pages=10,
        rate_limit_rps=1000,
        llms_txt="auto",
    )
    client = make_client(handler)
    pages = list(crawl(source, client=client))

    # Each linked doc page is yielded as an HTML item (html set, markdown absent),
    # so downstream extraction runs on the real page — NOT the index link-list.
    assert [p["url"] for p in pages] == [
        "https://example.com/docs/quickstart",
        "https://example.com/docs/config",
        "https://example.com/docs/api",
    ]
    for p in pages:
        assert p["fetch_ok"] is True
        assert "html" in p and p["html"] == PAGE_HTML
        assert "markdown" not in p
    # The pages were actually fetched over HTTP.
    assert "https://example.com/docs/quickstart" in requested


def test_llms_index_links_filtered_by_include_prefixes():
    """Index-derived URLs are scoped by include/exclude prefixes, like sitemap
    discovery — an out-of-scope link is not crawled."""
    index = (
        "# Docs\n\n## S\n"
        "- [in](https://example.com/docs/in)\n"
        "- [out](https://example.com/blog/out)\n"
        "- [also](https://example.com/docs/also)\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        if url.endswith("/llms-full.txt"):
            return httpx.Response(404, text="nope")
        if url.endswith("/llms.txt"):
            return httpx.Response(200, text=index)
        return httpx.Response(200, text=PAGE_HTML)

    source = SourceConfig(
        name="example",
        base_url="https://example.com/docs/",
        include_prefixes=["/docs/"],
        max_pages=10,
        rate_limit_rps=1000,
        llms_txt="auto",
    )
    client = make_client(handler)
    pages = list(crawl(source, client=client))
    assert [p["url"] for p in pages] == [
        "https://example.com/docs/in",
        "https://example.com/docs/also",
    ]


def test_llms_full_content_still_yields_markdown_sections():
    """A real `/llms-full.txt` is still split into already-extracted markdown
    sections (the fast path), not crawled page-by-page."""
    fetched_pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        if url.endswith("/llms-full.txt"):
            return httpx.Response(200, text=LLMS_FULL)
        fetched_pages.append(url)
        return httpx.Response(200, text=PAGE_HTML)

    source = SourceConfig(
        name="example",
        base_url="https://example.com/",
        max_pages=10,
        rate_limit_rps=1000,
        llms_txt="auto",
    )
    client = make_client(handler)
    pages = list(crawl(source, client=client))
    assert pages, "expected llms-full sections"
    for p in pages:
        assert "markdown" in p and p["markdown"]
        assert "html" not in p
    # No individual doc pages were HTTP-fetched — the full file was the source.
    assert fetched_pages == []


def test_llms_only_with_index_crawls_links_not_bfs():
    """`llms_txt="only"` + an index: crawl exactly the index's links, and do
    NOT fall back to a BFS crawl of the site."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        if url.endswith("/llms-full.txt"):
            return httpx.Response(404, text="nope")
        if url.endswith("/llms.txt"):
            return httpx.Response(200, text=LLMS_INDEX)
        # A page whose HTML links elsewhere — BFS would follow these.
        return httpx.Response(
            200,
            text='<html><body><a href="https://example.com/other/x">x</a>content</body></html>',
        )

    source = SourceConfig(
        name="example",
        base_url="https://example.com/",
        max_pages=10,
        rate_limit_rps=1000,
        llms_txt="only",
    )
    client = make_client(handler)
    pages = list(crawl(source, client=client))
    urls = [p["url"] for p in pages]
    # Only the three index links, no BFS-discovered /other/x.
    assert urls == [
        "https://example.com/docs/quickstart",
        "https://example.com/docs/config",
        "https://example.com/docs/api",
    ]


def test_crawl_is_generator_and_preserves_fetch_ok_false_contract():
    """Both load-bearing invariants asserted together: crawl() stays a
    generator (memory bounding / per-page commit), and an attempted-but-
    failed fetch is YIELDED with fetch_ok=False rather than dropped, so
    store.sync_source does not purge good rows on a transient failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        return httpx.Response(503)

    source = SourceConfig(
        name="example",
        base_url="https://example.com/",
        max_pages=10,
        rate_limit_rps=1000,
    )
    result = crawl(source, client=make_client(handler))
    assert inspect.isgenerator(result)
    pages = list(result)
    assert pages == [{"url": "https://example.com/", "html": None, "fetch_ok": False}]


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
    from app.crawler import extract_links
    from bs4 import XMLParsedAsHTMLWarning

    xml_content = '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.com/page1</loc></url></urlset>'
    extract_links(xml_content, "https://example.com/sitemap.xml")
    assert not [w for w in recwarn if issubclass(w.category, XMLParsedAsHTMLWarning)]



LLMS_FULL_THREE_SECTIONS = "# One\ncontent1\n\n# Two\ncontent2\n\n# Three\ncontent3\n"


def test_llms_auto_falls_back_to_bfs_when_discover_finds_nothing():
    """llms_txt="auto" with no llms-full.txt/llms.txt available (both 404)
    must fall through to the normal sitemap/BFS HTML crawl."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        if url in ("https://example.com/llms-full.txt", "https://example.com/llms.txt"):
            return httpx.Response(404)
        if url == "https://example.com/":
            return httpx.Response(200, text='<html><body><a href="/docs/a">A</a></body></html>')
        if url == "https://example.com/docs/a":
            return httpx.Response(200, text=PAGE_HTML)
        return httpx.Response(404)

    source = SourceConfig(
        name="example",
        base_url="https://example.com/",
        max_pages=10,
        rate_limit_rps=1000,
        llms_txt="auto",
    )
    client = make_client(handler)
    pages = list(crawl(source, client=client))
    urls = {p["url"] for p in pages}
    assert "https://example.com/" in urls
    assert "https://example.com/docs/a" in urls
    # BFS items carry "html", not "markdown".
    assert all("html" in p for p in pages)


def test_llms_only_yields_nothing_when_discover_finds_nothing():
    """llms_txt="only" with no llms-full.txt/llms.txt available must yield
    nothing at all — no BFS fallback."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        return httpx.Response(404)

    source = SourceConfig(
        name="example",
        base_url="https://example.com/",
        max_pages=10,
        rate_limit_rps=1000,
        llms_txt="only",
    )
    client = make_client(handler)
    pages = list(crawl(source, client=client))
    assert pages == []


def test_llms_discovered_body_yields_one_markdown_item_per_section_capped():
    """A discovered llms-full.txt body (first non-blank line an H1) yields
    one markdown item per split section, capped at source.max_pages."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        if url == "https://example.com/llms-full.txt":
            return httpx.Response(200, text=LLMS_FULL_THREE_SECTIONS)
        return httpx.Response(404)

    source = SourceConfig(
        name="example",
        base_url="https://example.com/",
        max_pages=2,
        rate_limit_rps=1000,
        llms_txt="auto",
    )
    client = make_client(handler)
    pages = list(crawl(source, client=client))
    assert len(pages) == 2
    for p in pages:
        assert p["fetch_ok"] is True
        assert "markdown" in p
        assert "heading_path" in p


def test_conditional_304_on_html_url_yields_not_modified_with_no_html():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        if url == "https://example.com/":
            if request.headers.get("if-none-match") == "abc123":
                return httpx.Response(304)
            return httpx.Response(200, text=PAGE_HTML)
        return httpx.Response(404)

    source = SourceConfig(
        name="example",
        base_url="https://example.com/",
        max_pages=10,
        rate_limit_rps=1000,
        llms_txt="off",
    )
    client = make_client(handler)
    pages = list(
        crawl(source, client=client, conditional={"https://example.com/": ("abc123", None)})
    )
    assert pages == [{"url": "https://example.com/", "not_modified": True, "fetch_ok": True}]


def test_200_html_response_surfaces_etag_and_last_modified_on_item():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        if url == "https://example.com/":
            return httpx.Response(
                200,
                text=PAGE_HTML,
                headers={"ETag": "xyz789", "Last-Modified": "Wed, 01 Jan 2020 00:00:00 GMT"},
            )
        return httpx.Response(404)

    source = SourceConfig(
        name="example",
        base_url="https://example.com/",
        max_pages=10,
        rate_limit_rps=1000,
        llms_txt="off",
    )
    client = make_client(handler)
    pages = list(crawl(source, client=client))
    assert len(pages) == 1
    assert pages[0]["etag"] == "xyz789"
    assert pages[0]["last_modified"] == "Wed, 01 Jan 2020 00:00:00 GMT"


def test_conditional_headers_only_sent_on_first_hop_not_redirect_target():
    requested_headers: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        if url == "https://example.com/docs/old":
            requested_headers["old"] = dict(request.headers)
            return httpx.Response(301, headers={"Location": "/docs/new"})
        if url == "https://example.com/docs/new":
            requested_headers["new"] = dict(request.headers)
            return httpx.Response(200, text=PAGE_HTML)
        return httpx.Response(404)

    source = SourceConfig(
        name="example",
        base_url="https://example.com/docs/old",
        max_pages=10,
        rate_limit_rps=1000,
        llms_txt="off",
    )
    client = make_client(handler)
    conditional = {
        "https://example.com/docs/old": ("etag-old", None),
        "https://example.com/docs/new": ("etag-new", None),
    }
    pages = list(crawl(source, client=client, conditional=conditional))
    assert len(pages) == 1
    assert requested_headers["old"].get("if-none-match") == "etag-old"
    assert "if-none-match" not in requested_headers["new"]


def test_llms_index_unchanged_sentinel_emitted_on_304_with_stored_validator():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        if url == "https://example.com/llms-full.txt":
            if request.headers.get("if-none-match") == "etag-full":
                return httpx.Response(304)
            return httpx.Response(404)
        return httpx.Response(404)

    source = SourceConfig(
        name="example",
        base_url="https://example.com/",
        max_pages=10,
        rate_limit_rps=1000,
        llms_txt="auto",
    )
    client = make_client(handler)
    conditional = {"https://example.com/llms-full.txt": ("etag-full", None)}
    pages = list(crawl(source, client=client, conditional=conditional))
    assert pages == [
        {
            "kind": "llms_index_unchanged",
            "url": "https://example.com/llms-full.txt",
            "not_modified": True,
            "fetch_ok": True,
        }
    ]


def test_crawl_refuses_unresolvable_host_fail_closed(monkeypatch):
    """Crawl time fails CLOSED on an unresolvable host (unlike config
    validation, which deliberately does not — see test_config)."""
    import socket as _socket

    from app.urlscope import _resolve_host_addrs

    _resolve_host_addrs.cache_clear()

    def fake_getaddrinfo(host, *args, **kwargs):
        raise _socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(_socket, "getaddrinfo", fake_getaddrinfo)

    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=PAGE_HTML)

    source = SourceConfig.model_construct(
        name="gone",
        base_url="https://gone.invalid/",
        sitemap=None,
        include_prefixes=[],
        exclude_prefixes=[],
        max_pages=10,
        language="english",
        rate_limit_rps=1000,
    )
    try:
        pages = list(crawl(source, client=make_client(handler)))
        assert pages == []
        assert requested == []
    finally:
        _resolve_host_addrs.cache_clear()
