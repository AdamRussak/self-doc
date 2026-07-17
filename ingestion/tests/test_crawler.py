import httpx

from app.config import SourceConfig
from app.crawler import _is_private_ip_host, _validate_final_url, crawl

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
    pages = crawl(source, client=client)
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
    pages = crawl(source, client=client)
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
    pages = crawl(source, client=client)
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
    pages = crawl(source, client=client)
    assert pages == []


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
    pages = crawl(source, client=client)
    assert pages == []


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
    pages = crawl(source, client=client)
    assert pages == []
