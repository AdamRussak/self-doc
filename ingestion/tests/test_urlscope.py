import pytest

from app.urlscope import parse_sitemap, path_allowed

# --- path_allowed ---------------------------------------------------------


def test_include_prefix_accepts_exact_and_subpath():
    assert path_allowed("/traefik", ["/traefik"], [])
    assert path_allowed("/traefik/routing", ["/traefik"], [])


def test_include_prefix_rejects_similarly_named_sibling_products():
    # These share a string prefix with '/traefik' but are different products
    # under a bare str.startswith match — must be rejected.
    assert not path_allowed("/traefik-hub/x", ["/traefik"], [])
    assert not path_allowed("/traefik-enterprise/y", ["/traefik"], [])
    assert not path_allowed("/traefik-mesh/z", ["/traefik"], [])


def test_include_prefix_off_by_one_at_path_end():
    # '/traefikx' is not '/traefik' followed by a boundary.
    assert not path_allowed("/traefikx", ["/traefik"], [])
    # But exactly '/traefik' (no trailing content) is allowed.
    assert path_allowed("/traefik", ["/traefik"], [])


def test_exclude_prefix_uses_substring_semantics_not_boundary():
    # Excludes intentionally use plain substring/startswith matching, NOT
    # the segment-boundary rule includes use — this is the documented
    # include/exclude asymmetry. An exclude of '/traefik/beta' therefore
    # DOES reject '/traefik/betamax' too (over-matching an exclude is safe:
    # it just skips a page, unlike over-matching an include which would
    # admit a whole wrong product).
    assert not path_allowed("/traefik/betamax", ["/traefik"], ["/traefik/beta"])
    assert not path_allowed("/traefik/beta", ["/traefik"], ["/traefik/beta"])
    assert not path_allowed("/traefik/beta/x", ["/traefik"], ["/traefik/beta"])


def test_exclude_prefix_rejects_versioned_segments_critical3():
    # Critical 3 (formal review): exclude_prefixes ["/traefik/v1", "/traefik/v2"]
    # must reject real versioned doc paths like /traefik/v2.11/... and
    # /traefik/v1.7/... even though these are NOT full-segment matches
    # under boundary rules (the segment is "v2.11", not "v2"). This is the
    # scenario boundary-anchored excludes would silently break.
    include = ["/traefik/"]
    exclude = ["/traefik/v1", "/traefik/v2"]
    assert not path_allowed("/traefik/v2.11/routing", include, exclude)
    assert not path_allowed("/traefik/v1.7/basics", include, exclude)
    # Unversioned current docs remain accepted.
    assert path_allowed("/traefik/routing/overview", include, exclude)
    # Migration guides mention v1/v2/v3 in their own slugs but are
    # legitimately in-scope content, not old-version doc trees — must NOT
    # be caught by the "/traefik/v1"/"/traefik/v2" excludes.
    assert path_allowed("/traefik/migrate/v2-to-v3/", include, exclude)
    assert path_allowed("/traefik/migration/v2-to-v3/", include, exclude)
    # Original scope-leak fix (sibling products) must not regress.
    assert not path_allowed("/traefik-hub/x", include, exclude)
    assert not path_allowed("/traefik-enterprise/y", include, exclude)
    assert not path_allowed("/traefik-mesh/z", include, exclude)


def test_exclude_wins_over_include():
    assert not path_allowed("/traefik/internal", ["/traefik"], ["/traefik/internal"])


def test_empty_include_list_allows_all_except_excluded():
    assert path_allowed("/anything/goes", [], [])
    assert not path_allowed("/blocked/x", [], ["/blocked"])


def test_trailing_slash_in_configured_include_prefix_is_normalized():
    # include_prefixes use boundary matching: trailing slash is normalized
    # away, and '/latestish' (no boundary) is correctly rejected.
    assert path_allowed("/latest", ["/latest/"], [])
    assert path_allowed("/latest/page", ["/latest/"], [])
    assert not path_allowed("/latestish", ["/latest/"], [])


def test_trailing_slash_in_configured_exclude_prefix_is_normalized():
    # A trailing slash on a configured exclude prefix is normalized away
    # before matching (same normalization as includes), so it does NOT
    # change whether the bare path itself is excluded. Excludes still use
    # substring semantics (no boundary requirement), so '/latestish' is
    # excluded regardless of whether the configured prefix carries a
    # trailing slash.
    assert not path_allowed("/latest", [], ["/latest/"])
    assert not path_allowed("/latest/page", [], ["/latest/"])
    assert not path_allowed("/latestish", [], ["/latest/"])

    assert not path_allowed("/latest", [], ["/latest"])
    assert not path_allowed("/latest/page", [], ["/latest"])
    assert not path_allowed("/latestish", [], ["/latest"])


def test_exclude_trailing_slash_does_not_change_bare_path_exclusion():
    # Warning-1 regression: exclude_prefixes: ["/compose/releases/"] must
    # exclude the bare path "/compose/releases" itself, not just its
    # children — a trailing slash in config must not let the top-level
    # page fail open.
    assert not path_allowed("/compose/releases", ["/compose"], ["/compose/releases/"])
    assert not path_allowed("/compose/releases/", ["/compose"], ["/compose/releases/"])
    assert not path_allowed("/compose/releases/v2", ["/compose"], ["/compose/releases/"])
    # Sanity: without the trailing slash in config, same result.
    assert not path_allowed("/compose/releases", ["/compose"], ["/compose/releases"])
    assert not path_allowed("/compose/releases/v2", ["/compose"], ["/compose/releases"])


def test_root_prefix_matches_everything():
    assert path_allowed("/", ["/"], [])
    assert path_allowed("/anything", ["/"], [])


def test_multiple_include_prefixes_any_match_wins():
    assert path_allowed("/docs/a", ["/docs", "/guides"], [])
    assert path_allowed("/guides/b", ["/docs", "/guides"], [])
    assert not path_allowed("/blog/c", ["/docs", "/guides"], [])


# --- parse_sitemap ---------------------------------------------------------

URLSET_NAMESPACED = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>  https://example.com/docs/a  </loc></url>
  <url><loc>https://example.com/docs/b</loc></url>
</urlset>
"""

URLSET_PLAIN = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset>
  <url><loc>https://example.com/docs/a</loc></url>
  <url><loc>https://example.com/docs/b</loc></url>
</urlset>
"""

SITEMAPINDEX_NAMESPACED = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://appwrite.io/sitemap-1.xml</loc></sitemap>
  <sitemap><loc>  https://appwrite.io/sitemap-2.xml  </loc></sitemap>
</sitemapindex>
"""

SITEMAPINDEX_PLAIN = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex>
  <sitemap><loc>https://appwrite.io/sitemap-1.xml</loc></sitemap>
  <sitemap><loc>https://appwrite.io/sitemap-2.xml</loc></sitemap>
</sitemapindex>
"""


def test_parse_urlset_namespaced():
    urls, children = parse_sitemap(URLSET_NAMESPACED)
    assert urls == ["https://example.com/docs/a", "https://example.com/docs/b"]
    assert children == []


def test_parse_urlset_plain():
    urls, children = parse_sitemap(URLSET_PLAIN)
    assert urls == ["https://example.com/docs/a", "https://example.com/docs/b"]
    assert children == []


def test_parse_sitemapindex_namespaced():
    urls, children = parse_sitemap(SITEMAPINDEX_NAMESPACED)
    assert urls == []
    assert children == [
        "https://appwrite.io/sitemap-1.xml",
        "https://appwrite.io/sitemap-2.xml",
    ]


def test_parse_sitemapindex_plain():
    urls, children = parse_sitemap(SITEMAPINDEX_PLAIN)
    assert urls == []
    assert children == [
        "https://appwrite.io/sitemap-1.xml",
        "https://appwrite.io/sitemap-2.xml",
    ]


def test_parse_sitemap_empty_input_raises():
    with pytest.raises(ValueError):
        parse_sitemap(b"")


def test_parse_sitemap_malformed_xml_raises():
    with pytest.raises(ValueError):
        parse_sitemap(b"<urlset><url><loc>unterminated")


def test_parse_sitemap_unexpected_root_raises():
    with pytest.raises(ValueError):
        parse_sitemap(b"<rss><channel></channel></rss>")
