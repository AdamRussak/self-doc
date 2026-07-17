import pytest

from app.embedder import EMBEDDING_DIM, embed_chunks


class _FakeModel:
    """Stand-in for fastembed.TextEmbedding — asserts passage_embed (not
    query_embed / embed) is the method used, per the asymmetric-embedding
    contract."""

    def __init__(self):
        self.calls = []

    def passage_embed(self, texts):
        self.calls.append(list(texts))
        for _ in texts:
            yield [0.1] * EMBEDDING_DIM

    def query_embed(self, texts):  # pragma: no cover - must never be called here
        raise AssertionError("embedder must use passage_embed, not query_embed")


def test_embed_chunks_uses_passage_embed_and_sets_384_dim_vectors():
    fake = _FakeModel()
    chunks = [
        {"url": "u", "heading_path": "H", "chunk_index": 0, "content": "hello world"},
        {"url": "u", "heading_path": "H", "chunk_index": 1, "content": "second chunk"},
    ]
    out = embed_chunks(chunks, model=fake)
    assert out is chunks
    for c in out:
        assert "embedding" in c
        assert len(c["embedding"]) == EMBEDDING_DIM
        assert all(isinstance(v, float) for v in c["embedding"])
    assert fake.calls  # passage_embed was invoked


def test_embed_chunks_batches(monkeypatch):
    fake = _FakeModel()
    chunks = [{"url": "u", "heading_path": "H", "chunk_index": i, "content": f"c{i}"} for i in range(70)]
    embed_chunks(chunks, model=fake, batch_size=32)
    # 70 items in batches of 32 => 3 calls (32, 32, 6)
    assert [len(c) for c in fake.calls] == [32, 32, 6]


def test_embed_chunks_empty_list_is_noop():
    fake = _FakeModel()
    assert embed_chunks([], model=fake) == []
    assert fake.calls == []
