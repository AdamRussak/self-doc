from app.retrieval import (
    ARM_CANDIDATE_LIMIT,
    HYBRID_SEARCH_SQL,
    RRF_K,
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
