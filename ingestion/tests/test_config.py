import socket
from pathlib import Path

import pytest
from app.config import ConfigError, load_sources
from app.urlscope import _resolve_host_addrs


def write_yaml(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "sources.yaml"
    p.write_text(text)
    return p
def test_load_sources_from_yaml(tmp_path):
    p = write_yaml(
        tmp_path,
        "sources:\n  - name: fastapi\n    base_url: https://fastapi.tiangolo.com/\n    max_pages: 10\n",
    )
    sources = load_sources(p)
    assert len(sources) == 1
    assert sources[0].name == "fastapi"
def test_valid_minimal_source(tmp_path):
    p = write_yaml(
        tmp_path,
        """
        sources:
          - name: my-docs
            base_url: https://example.com/docs
            max_pages: 10
        """,
    )
    sources = load_sources(p)
    assert len(sources) == 1
    s = sources[0]
    assert s.name == "my-docs"
    assert s.rate_limit_rps == 1.0
    assert s.language == "english"
    assert s.include_prefixes == []
    assert s.exclude_prefixes == []


def test_duplicate_name_raises(tmp_path):
    p = write_yaml(
        tmp_path,
        """
        sources:
          - name: dupe
            base_url: https://example.com/a
            max_pages: 5
          - name: dupe
            base_url: https://example.com/b
            max_pages: 5
        """,
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_sources(p)


def test_invalid_base_url_raises(tmp_path):
    p = write_yaml(
        tmp_path,
        """
        sources:
          - name: bad-url
            base_url: not-a-url
            max_pages: 5
        """,
    )
    with pytest.raises(ConfigError):
        load_sources(p)


def test_unknown_key_raises(tmp_path):
    p = write_yaml(
        tmp_path,
        """
        sources:
          - name: extra-key
            base_url: https://example.com
            max_pages: 5
            bogus_field: true
        """,
    )
    with pytest.raises(ConfigError):
        load_sources(p)


def test_invalid_name_pattern_raises(tmp_path):
    p = write_yaml(
        tmp_path,
        """
        sources:
          - name: Not_Valid_Name
            base_url: https://example.com
            max_pages: 5
        """,
    )
    with pytest.raises(ConfigError):
        load_sources(p)


def test_missing_max_pages_is_allowed_and_means_unlimited(tmp_path):
    # max_pages is optional: omitting it is valid and means "no page limit".
    p = write_yaml(
        tmp_path,
        """
        sources:
          - name: no-max-pages
            base_url: https://example.com
        """,
    )
    sources = load_sources(p)
    assert len(sources) == 1
    assert sources[0].max_pages is None


def test_zero_or_negative_max_pages_still_raises(tmp_path):
    # When provided, max_pages must be positive (gt=0).
    p = write_yaml(
        tmp_path,
        """
        sources:
          - name: bad-max-pages
            base_url: https://example.com
            max_pages: 0
        """,
    )
    with pytest.raises(ConfigError):
        load_sources(p)


def test_base_url_excluded_by_own_include_prefixes_raises(tmp_path):
    # Regression test for the nextjs lesson: base_url path `/docs` combined
    # with include_prefixes `["/docs/"]` filters out the only BFS seed URL,
    # so the source would silently index 0 pages. Must fail fast at load.
    p = write_yaml(
        tmp_path,
        """
        sources:
          - name: bad-seed
            base_url: https://example.com/docs
            include_prefixes: ["/docs/"]
            max_pages: 10
        """,
    )
    with pytest.raises(ConfigError, match="bad-seed"):
        load_sources(p)


def test_base_url_excluded_by_own_exclude_prefixes_raises(tmp_path):
    p = write_yaml(
        tmp_path,
        """
        sources:
          - name: bad-seed-excluded
            base_url: https://example.com/blog
            exclude_prefixes: ["/blog"]
            max_pages: 10
        """,
    )
    with pytest.raises(ConfigError, match="bad-seed-excluded"):
        load_sources(p)


def test_base_url_allowed_by_own_prefixes_loads_fine(tmp_path):
    p = write_yaml(
        tmp_path,
        """
        sources:
          - name: good-seed
            base_url: https://example.com/docs
            include_prefixes: ["/docs"]
            max_pages: 10
        """,
    )
    sources = load_sources(p)
    assert sources[0].name == "good-seed"


def test_sitemap_source_skips_base_url_prefix_check(tmp_path):
    # A sitemap-based source discovers URLs from the sitemap, not by BFS from
    # base_url, so base_url need not itself pass include_prefixes.
    p = write_yaml(
        tmp_path,
        """
        sources:
          - name: sitemap-src
            base_url: https://example.com/
            sitemap: https://example.com/sitemap.xml
            include_prefixes: ["/tutorial/"]
            max_pages: 10
        """,
    )
    sources = load_sources(p)
    assert sources[0].name == "sitemap-src"


# --- SSRF guard at validation time (security review H1/H2) ----------------
#
# Source URLs are untrusted input (admin web form + an MCP tool callable by
# an AI agent), so a bad proposal must be rejected BEFORE a human is shown an
# approval prompt.


def _yaml_source(**kv) -> str:
    lines = ["sources:", "  - name: widget"]
    for k, v in kv.items():
        lines.append(f"    {k}: {v}")
    lines.append("    max_pages: 10")
    return "\n".join(lines) + "\n"


@pytest.fixture(autouse=True)
def _clear_resolver_cache():
    _resolve_host_addrs.cache_clear()
    yield
    _resolve_host_addrs.cache_clear()


def _fake_getaddrinfo(mapping):
    def fake(host, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror(-2, "Name or service not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], 0))]

    return fake


def test_sitemap_on_different_host_than_base_url_is_rejected(tmp_path):
    """H1: the sitemap is fetched before any of its <loc> entries are
    host-filtered, so an off-host sitemap is a direct SSRF vector."""
    p = write_yaml(tmp_path, _yaml_source(
        base_url="https://widget.example.com/",
        sitemap="http://169.254.169.254/latest/meta-data/",
    ))
    with pytest.raises(ConfigError) as e:
        load_sources(p)
    assert "sitemap host" in str(e.value)


def test_sitemap_on_same_host_is_accepted(tmp_path):
    p = write_yaml(tmp_path, _yaml_source(
        base_url="https://widget.example.com/",
        sitemap="https://widget.example.com/sitemap.xml",
    ))
    sources = load_sources(p)
    assert str(sources[0].sitemap) == "https://widget.example.com/sitemap.xml"


def test_private_literal_base_url_is_rejected(tmp_path):
    p = write_yaml(tmp_path, _yaml_source(base_url="http://192.168.1.10/"))
    with pytest.raises(ConfigError) as e:
        load_sources(p)
    assert "private" in str(e.value)


def test_decimal_encoded_loopback_base_url_is_rejected(tmp_path, monkeypatch):
    """`ipaddress.ip_address("2130706433")` raises — a literal-only check
    lets this through. The resolver normalizes it to 127.0.0.1."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"2130706433": "127.0.0.1"}))
    p = write_yaml(tmp_path, _yaml_source(base_url="http://2130706433/"))
    with pytest.raises(ConfigError) as e:
        load_sources(p)
    assert "private" in str(e.value)


def test_public_hostname_resolving_into_private_space_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({
        "docs.attacker.example": "10.0.0.5",
    }))
    p = write_yaml(tmp_path, _yaml_source(base_url="https://docs.attacker.example/"))
    with pytest.raises(ConfigError) as e:
        load_sources(p)
    assert "private" in str(e.value)


def test_private_sitemap_is_rejected_even_when_host_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({
        "docs.attacker.example": "127.0.0.1",
    }))
    p = write_yaml(tmp_path, _yaml_source(
        base_url="https://docs.attacker.example/",
        sitemap="https://docs.attacker.example/sitemap.xml",
    ))
    with pytest.raises(ConfigError) as e:
        load_sources(p)
    assert "private" in str(e.value)


def test_unresolvable_host_is_not_rejected_at_validation_time(tmp_path, monkeypatch):
    """Deliberate: validation fetches nothing, so a DNS blip must not
    permanently reject a legitimate source. `crawl()` still fails closed and
    refuses to fetch an unresolvable host (see test_crawler)."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({}))
    p = write_yaml(tmp_path, _yaml_source(base_url="https://widget.example.com/"))
    assert load_sources(p)[0].name == "widget"
