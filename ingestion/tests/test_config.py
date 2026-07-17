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
