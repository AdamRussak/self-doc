from pathlib import Path

import pytest

from app.config import ConfigError, load_sources


def write_yaml(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "sources.yaml"
    p.write_text(text)
    return p


def test_seed_sources_yaml_loads(tmp_path):
    seed = Path(__file__).parent.parent / "app" / "sources.yaml"
    sources = load_sources(seed)
    names = {s.name for s in sources}
    assert names == {"fastapi", "nextjs", "pgvector-readme"}
    for s in sources:
        assert s.max_pages > 0
        assert s.rate_limit_rps > 0
        assert s.language == "english"


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


def test_missing_max_pages_raises(tmp_path):
    p = write_yaml(
        tmp_path,
        """
        sources:
          - name: no-max-pages
            base_url: https://example.com
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
