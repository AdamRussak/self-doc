"""FastEmbed passage-embedding wrapper.

BAAI/bge-small-en-v1.5 is asymmetric: documents MUST be embedded with
`passage_embed()` (FastEmbed applies the BGE `passage:` prefix), while
queries use `query_embed()` — that half is the mcp-server's job, not
ingestion's. Skipping the passage prefix measurably hurts recall.
"""

from __future__ import annotations

import threading

from fastembed import TextEmbedding

from .logging_config import get_logger

MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384
BATCH_SIZE = 32

logger = get_logger(component="embedder")

_model_lock = threading.Lock()
_model: TextEmbedding | None = None


def get_model() -> TextEmbedding:
    """Lazily load (and cache) the shared FastEmbed model instance."""
    global _model
    with _model_lock:
        if _model is None:
            logger.info("loading_embedding_model", model=MODEL_NAME)
            _model = TextEmbedding(model_name=MODEL_NAME)
        return _model


def embed_chunks(chunks: list[dict], model: TextEmbedding | None = None, batch_size: int = BATCH_SIZE) -> list[dict]:
    """Add an `embedding: list[float]` (len 384) field to each chunk dict via
    passage_embed, in batches of `batch_size`. Returns the same list of dicts
    (mutated in place) for convenience.
    """
    if not chunks:
        return chunks

    m = model or get_model()
    texts = [c["content"] for c in chunks]

    embeddings: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        for vec in m.passage_embed(batch):
            embeddings.append(vec.tolist() if hasattr(vec, "tolist") else list(vec))

    if len(embeddings) != len(chunks):
        raise RuntimeError(
            f"embedding count mismatch: got {len(embeddings)} vectors for {len(chunks)} chunks"
        )

    for chunk, vec in zip(chunks, embeddings):
        if len(vec) != EMBEDDING_DIM:
            raise RuntimeError(f"unexpected embedding dim {len(vec)} (expected {EMBEDDING_DIM})")
        chunk["embedding"] = vec

    logger.info("embedded_chunks", count=len(chunks))
    return chunks
