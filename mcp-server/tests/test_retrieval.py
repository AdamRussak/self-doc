import pytest
from app.retrieval import (
    ARM_CANDIDATE_LIMIT,
    HYBRID_SEARCH_SQL,
    MAX_UPLOAD_CONTENT_BYTES,
    MAX_UPLOAD_TITLE_CHARS,
    RRF_K,
    ProposedSourceConfig,
    UploadError,
    _chunk_upload_content,
    _format_vector_literal,
    format_hit,
    upload_text,
)
from pydantic import ValidationError


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
        "base_url": "https://docs.python.org/docs/",
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


# ---------------------------------------------------------------------------
# upload_text (T11) — size-cap rejections run before any DB access, so these
# are plain unit tests (no live Postgres required). Unknown-source/wrong-
# source_type/happy-path/no-doc_sources-write coverage lives in
# test_retrieval_integration.py (needs a real doc_sources row to resolve).
# ---------------------------------------------------------------------------


def test_upload_text_rejects_oversized_title():
    oversized_title = "x" * (MAX_UPLOAD_TITLE_CHARS + 1)
    with pytest.raises(UploadError, match="character cap"):
        upload_text(source="whatever", title=oversized_title, content="hello world")


def test_upload_text_accepts_title_at_exact_cap_boundary_before_db_lookup(monkeypatch):
    # At exactly MAX_UPLOAD_TITLE_CHARS the title check must NOT reject —
    # confirmed by observing the call proceed past validation to get_pool(),
    # which is monkeypatched here to raise a distinctive sentinel instead of
    # an UploadError about the title.
    class _PastValidation(Exception):
        pass

    def _sentinel_get_pool():
        raise _PastValidation

    monkeypatch.setattr("app.retrieval.get_pool", _sentinel_get_pool)
    boundary_title = "x" * MAX_UPLOAD_TITLE_CHARS
    with pytest.raises(_PastValidation):
        upload_text(source="whatever", title=boundary_title, content="hello world")


def test_upload_text_rejects_oversized_content():
    oversized_content = "x" * (MAX_UPLOAD_CONTENT_BYTES + 1)
    with pytest.raises(UploadError, match="byte"):
        upload_text(source="whatever", title="T", content=oversized_content)


def test_upload_text_rejects_no_partial_processing_on_oversized_content(monkeypatch):
    # Confirms the size check happens BEFORE get_pool() is ever called —
    # "no partial processing" means an oversized call must not touch the DB
    # at all, not even to look the source up.
    calls = []
    monkeypatch.setattr("app.retrieval.get_pool", lambda: calls.append("called"))
    oversized_content = "x" * (MAX_UPLOAD_CONTENT_BYTES + 1)
    with pytest.raises(UploadError):
        upload_text(source="whatever", title="T", content=oversized_content)
    assert calls == []


def test_chunk_upload_content_splits_by_heading():
    content = "# A\n\npara1\n\npara2\n\n## B\n\nmore content here"
    chunks = _chunk_upload_content(content)
    headings = [c["heading_path"] for c in chunks]
    assert "A" in headings
    assert "A > B" in headings


def test_chunk_upload_content_chunk_index_is_sequential():
    content = "# A\n\npara1\n\n## B\n\npara2\n\n## C\n\npara3"
    chunks = _chunk_upload_content(content)
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))


def test_chunk_upload_content_respects_char_budget():
    long_para = "word " * 1000  # ~5000 chars, well over the default budget
    other_para = "short paragraph"
    content = f"{long_para}\n\n{other_para}"
    chunks = _chunk_upload_content(content, char_budget=100)
    assert len(chunks) >= 2


def test_chunk_upload_content_empty_input_yields_no_chunks():
    assert _chunk_upload_content("") == []
    assert _chunk_upload_content("   \n\n  ") == []
