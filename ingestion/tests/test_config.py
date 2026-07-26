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
            base_url: https://docs-fixture.dev/docs
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
            base_url: https://docs-fixture.dev/a
            max_pages: 5
          - name: dupe
            base_url: https://docs-fixture.dev/b
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
            base_url: https://docs-fixture.dev
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
            base_url: https://docs-fixture.dev
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
            base_url: https://docs-fixture.dev
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
            base_url: https://docs-fixture.dev
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
            base_url: https://docs-fixture.dev/docs
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
            base_url: https://docs-fixture.dev/blog
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
            base_url: https://docs-fixture.dev/docs
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
            base_url: https://docs-fixture.dev/
            sitemap: https://docs-fixture.dev/sitemap.xml
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


# --- Placeholder/reserved documentation-example hosts (T13) ----------------
#
# A live-database audit found test/placeholder rows in the production index,
# including a page indexed under a trusted source name at
# 'https://docs.company.com/api-reference'. Reject RFC 2606/6761
# reserved/placeholder hosts at validation time so this class of row can
# never be re-added.
#
# NOTE: the assertion below checks for "reserved documentation-example host"
# (the placeholder validator's own wording), not the bare word "placeholder"
# — pytest's tmp_path for these parametrized tests is itself derived from the
# test's own name (which contains the substring "placeholder"), and that
# tmp_path is embedded in ConfigError's message, so a naive substring check
# on "placeholder" would pass for the wrong reason (matching the tmp file
# path, not the actual validation error).

# Hosts caught by _base_url_must_not_be_placeholder itself.
PLACEHOLDER_HOST_CASES = [
    "https://example.com/docs",
    "https://example.org/docs",
    "https://example.net/docs",
    "https://docs.company.com/docs",
    "https://foo.test/docs",
    "https://foo.invalid/docs",
    "https://foo.example/docs",
]

# Hosts that are ALSO placeholder hosts, but resolve to loopback for real (via
# this suite's DNS stub's _ALLOW_REAL_DNS_HOSTS passthrough for
# 'localhost'/'*.localhost'), so _hosts_must_not_be_private (declared earlier
# in SourceConfig, and therefore runs first) rejects them before the
# placeholder validator ever gets a chance to. Still rejected — just for a
# different, earlier-in-the-chain reason.
LOOPBACK_PLACEHOLDER_HOST_CASES = [
    "https://localhost/docs",
    "https://foo.localhost/docs",
]


@pytest.mark.parametrize("base_url", PLACEHOLDER_HOST_CASES)
def test_placeholder_base_url_is_rejected(tmp_path, base_url):
    p = write_yaml(tmp_path, _yaml_source(base_url=base_url))
    with pytest.raises(ConfigError) as e:
        load_sources(p)
    assert "reserved documentation-example host" in str(e.value)


@pytest.mark.parametrize("base_url", LOOPBACK_PLACEHOLDER_HOST_CASES)
def test_loopback_placeholder_base_url_is_rejected(tmp_path, base_url):
    p = write_yaml(tmp_path, _yaml_source(base_url=base_url))
    with pytest.raises(ConfigError) as e:
        load_sources(p)
    assert "private" in str(e.value)


@pytest.mark.parametrize("sitemap_host", ["example.com", "docs.company.com", "foo.test"])
def test_placeholder_sitemap_host_is_rejected(tmp_path, sitemap_host):
    # sitemap must share base_url's host to pass the H1 SSRF guard, so give
    # both the same placeholder host to isolate the placeholder rejection.
    p = write_yaml(tmp_path, _yaml_source(
        base_url=f"https://{sitemap_host}/",
        sitemap=f"https://{sitemap_host}/sitemap.xml",
    ))
    with pytest.raises(ConfigError) as e:
        load_sources(p)
    assert "reserved documentation-example host" in str(e.value)


def test_subdomain_of_placeholder_host_is_not_rejected(tmp_path):
    """Only exact placeholder hosts / their reserved suffixes are rejected —
    a real subdomain like widget.example.com is a legitimate-looking fixture
    host elsewhere in this suite and must keep loading fine."""
    p = write_yaml(tmp_path, _yaml_source(base_url="https://widget.example.com/"))
    assert load_sources(p)[0].name == "widget"


def test_legitimate_host_is_not_rejected_by_placeholder_check(tmp_path):
    p = write_yaml(tmp_path, _yaml_source(base_url="https://docs-fixture.dev/"))
    assert load_sources(p)[0].name == "widget"


def test_js_render_defaults_false(tmp_path):
    """T7: a source that doesn't mention `js_render` must behave exactly as
    before — no renderer call is ever made for it."""
    p = write_yaml(
        tmp_path,
        "sources:\n  - name: fastapi\n    base_url: https://fastapi.tiangolo.com/\n    max_pages: 10\n",
    )
    assert load_sources(p)[0].js_render is False


def test_js_render_can_be_opted_in(tmp_path):
    p = write_yaml(
        tmp_path,
        "sources:\n  - name: fastapi\n    base_url: https://fastapi.tiangolo.com/\n"
        "    max_pages: 10\n    js_render: true\n",
    )
    assert load_sources(p)[0].js_render is True
