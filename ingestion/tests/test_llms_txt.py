"""Unit tests for llms_txt.py: discover() (HTTP fetch, always caller-injected
client, never raises) and split_llms_full() (pure markdown splitting).

discover() tests use a fake httpx client (httpx.MockTransport) — no real
network access. split_llms_full() tests are pure and need no client at all.
"""

from __future__ import annotations

import httpx

from app.llms_txt import discover, split_llms_full


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
