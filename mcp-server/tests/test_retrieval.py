import pytest
from pydantic import ValidationError

from app.retrieval import (
    ARM_CANDIDATE_LIMIT,
    HYBRID_SEARCH_SQL,
    RRF_K,
    ProposedSourceConfig,
    _format_vector_literal,
    format_hit,
)


def test_format_hit_matches_contract():
    result = format_hit("Guide > Routing > Dynamic Routes", "https://example.com/x", "some content")
    assert result == (
        "### Guide > Routing > Dynamic Routes\n"
        "[https://example.com/x](https://example.com/x)\n\n"
        "some content"
    )


def test_format_hit_handles_missing_heading():
    result = format_hit(None, "https://example.com/x", "content")
    assert result.startswith("### \n")


def test_format_vector_literal():
    literal = _format_vector_literal([0.1, 0.2, 0.3])
    assert literal == "[0.1,0.2,0.3]"


def test_hybrid_sql_uses_rrf_k_and_arm_limit():
    assert f"LIMIT {ARM_CANDIDATE_LIMIT}" in HYBRID_SEARCH_SQL
    assert f"{RRF_K} + rnk" in HYBRID_SEARCH_SQL
    assert "websearch_to_tsquery" in HYBRID_SEARCH_SQL
    assert "vector_cosine" not in HYBRID_SEARCH_SQL  # index name isn't hardcoded; op used is <=>
    assert "<=>" in HYBRID_SEARCH_SQL


def test_hybrid_sql_fts_arm_uses_per_chunk_fts_config():
    # Both the ts_rank select and the WHERE ... fts @@ ... filter must drive
    # off dc.fts_config (per-chunk language), not a hardcoded 'english'
    # literal — otherwise non-English chunks never match their own language.
    assert "websearch_to_tsquery(dc.fts_config, %(query_text)s)" in HYBRID_SEARCH_SQL
    assert "websearch_to_tsquery('english'" not in HYBRID_SEARCH_SQL


def _base_source_kwargs(**overrides):
    kwargs = {
        "name": "example-docs",
        "base_url": "https://example.com/docs/",
        "max_pages": 10,
    }
    kwargs.update(overrides)
    return kwargs


def test_proposed_source_config_normalizes_language_case_and_whitespace():
    cfg = ProposedSourceConfig(**_base_source_kwargs(language="German"))
    assert cfg.language == "german"


def test_proposed_source_config_accepts_lowercase_language():
    cfg = ProposedSourceConfig(**_base_source_kwargs(language="french"))
    assert cfg.language == "french"


def test_proposed_source_config_rejects_unsupported_language():
    with pytest.raises(ValidationError):
        ProposedSourceConfig(**_base_source_kwargs(language="klingon"))


def test_proposed_source_config_accepts_llms_txt_only():
    cfg = ProposedSourceConfig(**_base_source_kwargs(llms_txt="only"))
    assert cfg.llms_txt == "only"


def test_proposed_source_config_rejects_invalid_llms_txt():
    with pytest.raises(ValidationError):
        ProposedSourceConfig(**_base_source_kwargs(llms_txt="bogus"))
