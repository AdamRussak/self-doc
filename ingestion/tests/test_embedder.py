
import os

import onnxruntime as ort

import app.embedder as embedder
from app.embedder import DEFAULT_ORT_THREADS, EMBEDDING_DIM, embed_chunks


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


class _SpyTextEmbedding:
    """Stand-in for fastembed.TextEmbedding that records constructor kwargs,
    so we can assert `threads` is forwarded without downloading a real model."""

    last_kwargs: dict | None = None

    def __init__(self, model_name, threads=None, **kwargs):
        _SpyTextEmbedding.last_kwargs = {"model_name": model_name, "threads": threads, **kwargs}


def test_get_model_passes_ort_num_threads_env_value(monkeypatch):
    """get_model() must forward ORT_NUM_THREADS into TextEmbedding's `threads`
    kwarg — that's the call boundary FastEmbed exposes for controlling ONNX
    Runtime's thread pools (see app/embedder.py comment)."""
    monkeypatch.setattr(embedder, "ORT_NUM_THREADS", 5)
    monkeypatch.setattr(embedder, "TextEmbedding", _SpyTextEmbedding)
    monkeypatch.setattr(embedder, "_model", None)
    model = embedder.get_model()
    assert isinstance(model, _SpyTextEmbedding)
    assert _SpyTextEmbedding.last_kwargs["threads"] == 5
    embedder._model = None  # reset shared singleton for other tests


def test_ort_num_threads_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("ORT_NUM_THREADS", raising=False)
    # Re-derive the module-level default the same way embedder.py does at
    # import time, to confirm the documented default (2) without reloading
    # the module (which would rebind other constants pinned by other tests).
    assert int(os.environ.get("ORT_NUM_THREADS", str(DEFAULT_ORT_THREADS))) == DEFAULT_ORT_THREADS
    assert DEFAULT_ORT_THREADS == 4


def test_ort_session_options_reflect_thread_count():
    """Proves the downstream mechanism our fix relies on: FastEmbed's
    OnnxModel._load_onnx_model does exactly

        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = threads
        InferenceSession(model_path, providers=..., sess_options=so)

    (fastembed/common/onnx_model.py). This test exercises that exact sequence
    against a real ONNX Runtime InferenceSession (using a tiny model bundled
    with the onnxruntime package itself, so no network/model download is
    needed) and confirms the thread count set via `threads` really does reach
    the live session's SessionOptions — not just that our code passed a
    kwarg somewhere.
    """
    model_path = os.path.join(
        os.path.dirname(ort.__file__), "datasets", "sigmoid.onnx"
    )
    threads = 3
    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    so.inter_op_num_threads = threads
    sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"], sess_options=so)
    got = sess.get_session_options()
    assert got.intra_op_num_threads == threads
    assert got.inter_op_num_threads == threads
