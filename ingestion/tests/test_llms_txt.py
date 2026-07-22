"""Unit tests for llms_txt.py: discover() (HTTP fetch, always caller-injected
client, never raises) and split_llms_full() (pure markdown splitting).

discover() tests use a fake httpx client (httpx.MockTransport) — no real
network access. split_llms_full() tests are pure and need no client at all.
"""

from __future__ import annotations

import httpx
from app.llms_txt import discover, looks_like_index, parse_llms_index, split_llms_full


def make_client(handler) -> httpx.Client:
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


# --- discover() --------------------------------------------------------


def test_discover_returns_none_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    client = make_client(handler)
    assert discover(client, "https://example.com/") is None


def test_discover_returns_none_when_over_max_bytes():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://example.com/llms-full.txt":
            return httpx.Response(200, text="x" * 100)
        return httpx.Response(404)

    client = make_client(handler)
    assert discover(client, "https://example.com/", max_bytes=10) is None


def test_discover_aborts_download_once_over_cap():
    """The oversized body must be abandoned mid-stream, not fully downloaded and
    then discarded — otherwise a 24MB llms-full.txt costs a 24MB transfer just
    to be rejected."""
    consumed = {"chunks": 0}

    def body_gen():
        for _ in range(1000):  # up to 1000 * 1KB = 1MB if fully read
            consumed["chunks"] += 1
            yield b"x" * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/llms-full.txt"):
            return httpx.Response(200, content=body_gen())
        return httpx.Response(404)

    client = make_client(handler)
    result = discover(client, "https://example.com/", max_bytes=5_000)  # 5KB cap
    assert result is None
    # Stopped shortly after crossing 5KB (~6 chunks), not the whole 1000.
    assert consumed["chunks"] < 20


def test_discover_accepts_body_at_the_cap_boundary():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/llms-full.txt"):
            return httpx.Response(200, text="# T\n" + "y" * 96)  # exactly 100 bytes
        return httpx.Response(404)

    client = make_client(handler)
    result = discover(client, "https://example.com/", max_bytes=100)
    assert result is not None
    assert result[0].endswith("/llms-full.txt")


def test_discover_prefers_llms_full_over_llms_txt():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://example.com/llms-full.txt":
            return httpx.Response(200, text="# Full\nfull body content")
        if url == "https://example.com/llms.txt":
            return httpx.Response(200, text="# Index\nindex body content")
        return httpx.Response(404)

    client = make_client(handler)
    result = discover(client, "https://example.com/")
    assert result is not None
    url, text = result
    assert url == "https://example.com/llms-full.txt"
    assert "Full" in text


# --- split_llms_full() --------------------------------------------------


def test_split_three_h1_sections_have_distinct_stable_slugged_urls_and_titles():
    source_url = "https://example.com/llms-full.txt"
    text = (
        "# Section One\ncontent1\n\n"
        "# Section Two\ncontent2\n\n"
        "# Section Three\ncontent3\n"
    )
    sections = split_llms_full(text, source_url)
    assert len(sections) == 3
    assert [s["heading_path"] for s in sections] == [
        "Section One",
        "Section Two",
        "Section Three",
    ]
    urls = [s["url"] for s in sections]
    assert len(set(urls)) == 3
    assert urls == [
        f"{source_url}#section-one",
        f"{source_url}#section-two",
        f"{source_url}#section-three",
    ]


def test_split_falls_back_to_h2_when_fewer_than_two_h1():
    source_url = "https://example.com/llms-full.txt"
    text = "# Title\nIntro text\n\n## Sub One\ncontent\n\n## Sub Two\ncontent\n"
    sections = split_llms_full(text, source_url)
    headings = [s["heading_path"] for s in sections]
    assert "Sub One" in headings
    assert "Sub Two" in headings
    # The lone H1 + intro text become the preamble section since the split
    # promoted to H2 level.
    assert sections[0]["heading_path"] == ""
    assert "# Title" in sections[0]["markdown"]


def test_split_whole_text_single_section_fallback_when_no_headings():
    source_url = "https://example.com/llms-full.txt"
    text = "Just plain text\nno headings at all\n"
    sections = split_llms_full(text, source_url)
    assert len(sections) == 1
    assert sections[0]["heading_path"] == ""
    assert sections[0]["url"] == f"{source_url}#preamble"
    assert sections[0]["markdown"] == text


def test_split_markdown_link_heading_yields_link_url():
    source_url = "https://x.example/llms-full.txt"
    text = "# [Routing](https://x/routing)\nbody text\n\n# Other\nmore\n"
    sections = split_llms_full(text, source_url)
    assert len(sections) == 2
    assert sections[0]["url"] == "https://x/routing"
    assert sections[0]["heading_path"] == "Routing"


def test_split_source_body_line_used_for_url():
    source_url = "https://x.example/llms-full.txt"
    text = "# Auth\nSource: https://docs.example.com/auth\nbody\n\n# Other\nmore\n"
    sections = split_llms_full(text, source_url)
    assert len(sections) == 2
    assert sections[0]["url"] == "https://docs.example.com/auth"
    assert sections[0]["heading_path"] == "Auth"


def test_split_fenced_block_hash_line_not_treated_as_heading():
    source_url = "https://example.com/llms-full.txt"
    text = (
        "# First\ncontent\n\n"
        "```\n# not a heading\n```\n\n"
        "# Second\ncontent\n"
    )
    sections = split_llms_full(text, source_url)
    assert len(sections) == 2
    assert sections[0]["heading_path"] == "First"
    assert sections[1]["heading_path"] == "Second"
    assert "# not a heading" in sections[0]["markdown"]


def test_split_is_deterministic_across_runs():
    source_url = "https://example.com/llms-full.txt"
    text = (
        "# Section One\ncontent1\n\n"
        "# Section Two\ncontent2\n\n"
        "# Section Three\ncontent3\n"
    )
    urls1 = [s["url"] for s in split_llms_full(text, source_url)]
    urls2 = [s["url"] for s in split_llms_full(text, source_url)]
    assert urls1 == urls2


# --- looks_like_index() / parse_llms_index() ----------------------------

INDEX_TXT = """# Example Docs

> A short summary of the project.

## Guides
- [Quickstart](https://example.com/docs/quickstart): get started
- [Configuration](https://example.com/docs/config)
- [Deployment](/docs/deploy): how to deploy

## Reference
- [API](https://example.com/docs/api): the API reference
"""

FULL_TXT = """# Quickstart

Install the package and import it. This paragraph is real documentation prose,
not a list of links, so the file is full content rather than an index.

```
pip install example
```

## Configuration

Configuration lives in a YAML file. Again this is prose describing behavior in
enough detail that it clearly is not a bullet list of links.
"""


def test_looks_like_index_true_for_link_list():
    assert looks_like_index(INDEX_TXT) is True


def test_looks_like_index_false_for_full_content():
    assert looks_like_index(FULL_TXT) is False


def test_looks_like_index_false_for_prose_with_a_couple_of_links():
    # A full page that merely contains one or two inline link bullets must not
    # be misclassified as an index (needs >=3 bullets AND a majority).
    text = (
        "# Overview\n\n"
        "This is a real page with lots of prose describing the system in detail "
        "across multiple sentences of genuine content.\n\n"
        "- [See also](https://example.com/x)\n"
        "More prose after the single link, continuing the explanation.\n"
    )
    assert looks_like_index(text) is False


def test_parse_llms_index_extracts_absolute_and_relative_urls_in_order():
    urls = parse_llms_index(INDEX_TXT, "https://example.com/")
    assert urls == [
        "https://example.com/docs/quickstart",
        "https://example.com/docs/config",
        "https://example.com/docs/deploy",
        "https://example.com/docs/api",
    ]


def test_parse_llms_index_dedupes_and_skips_fragments_and_non_http():
    text = (
        "## S\n"
        "- [A](https://example.com/a)\n"
        "- [A again](https://example.com/a)\n"
        "- [frag](#section)\n"
        "- [mail](mailto:x@example.com)\n"
    )
    assert parse_llms_index(text, "https://example.com/") == ["https://example.com/a"]


def test_parse_llms_index_ignores_links_inside_code_fences():
    text = (
        "## S\n"
        "- [real](https://example.com/real)\n"
        "```\n"
        "- [fenced](https://example.com/fenced)\n"
        "```\n"
    )
    assert parse_llms_index(text, "https://example.com/") == ["https://example.com/real"]
