
import app.embedder as embedder
from app.embedder import EMBEDDING_DIM, embed_chunks


class _FakeModel:
    """Stand-in for fastembed.TextEmbedding — records the exact texts passed to
    `embed`, so tests can assert the passage prompt is applied and that plain
    `embed` (not query_embed) is the method used."""

    def __init__(self):
        self.calls = []

    def embed(self, texts):
        texts = list(texts)
        self.calls.append(texts)
        for _ in texts:
            yield [0.1] * EMBEDDING_DIM


def test_embed_chunks_sets_dim_vectors():
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
    assert fake.calls  # embed was invoked


def test_embed_chunks_applies_passage_prompt(monkeypatch):
    monkeypatch.setattr(embedder, "PASSAGE_PROMPT", "passage: ")
    fake = _FakeModel()
    embed_chunks([{"content": "hello world"}], model=fake)
    assert fake.calls == [["passage: hello world"]]


def test_embed_chunks_empty_passage_prompt_leaves_text_unchanged(monkeypatch):
    monkeypatch.setattr(embedder, "PASSAGE_PROMPT", "")
    fake = _FakeModel()
    embed_chunks([{"content": "hello world"}], model=fake)
    assert fake.calls == [["hello world"]]


def test_embed_chunks_batches():
    fake = _FakeModel()
    chunks = [{"url": "u", "heading_path": "H", "chunk_index": i, "content": f"c{i}"} for i in range(70)]
    embed_chunks(chunks, model=fake, batch_size=32)
    # 70 items in batches of 32 => 3 calls (32, 32, 6)
    assert [len(c) for c in fake.calls] == [32, 32, 6]


def test_embed_chunks_empty_list_is_noop():
    fake = _FakeModel()
    assert embed_chunks([], model=fake) == []
    assert fake.calls == []
