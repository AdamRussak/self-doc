import httpx

from app.config import SourceConfig
from app.crawler import crawl

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
