"""FastEmbed passage-embedding wrapper.

The embedding model, its dimension, and the per-model PASSAGE prompt are all
env-driven (set by `make configure` from config/models.yaml), defaulting to the
registry default so a fresh checkout / CI works with no configuration:

    EMBEDDING_MODEL_NAME  default mixedbread-ai/mxbai-embed-large-v1
    EMBEDDING_DIM         default 1024
    EMBEDDING_PASSAGE_PROMPT  default "" (mxbai wants no passage prefix)

Asymmetric retrieval: many models want an instruction/prefix that DIFFERS
between documents and queries. FastEmbed's `passage_embed`/`query_embed` do NOT
apply any prefix for the models we support (bge, mxbai, e5 — they are not
"multitask" models), so we apply the prompt ourselves around plain `embed()`.
Documents get EMBEDDING_PASSAGE_PROMPT here; queries get EMBEDDING_QUERY_PROMPT
in the mcp-server (`retrieval._embed_query`). For mxbai/bge that means no
document prefix and an instruction on the query; for e5 both sides get a prefix.
Keep the two sides consistent — they must use the SAME model.
"""

from __future__ import annotations

import os
import threading

from fastembed import TextEmbedding

from .logging_config import get_logger

# Fallbacks used when EMBEDDING_* env is unset. These MUST equal the registry
# default row in config/models.yaml — tests/test_model_registry.py enforces it.
DEFAULT_MODEL_NAME = "mixedbread-ai/mxbai-embed-large-v1"
DEFAULT_EMBEDDING_DIM = 1024
DEFAULT_PASSAGE_PROMPT = ""

MODEL_NAME = os.environ.get("EMBEDDING_MODEL_NAME", DEFAULT_MODEL_NAME)
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", str(DEFAULT_EMBEDDING_DIM)))
PASSAGE_PROMPT = os.environ.get("EMBEDDING_PASSAGE_PROMPT", DEFAULT_PASSAGE_PROMPT)
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
    """Add an `embedding: list[float]` (len EMBEDDING_DIM) field to each chunk
    dict, in batches of `batch_size`. The document text is prefixed with
    EMBEDDING_PASSAGE_PROMPT (empty for mxbai/bge) before embedding. Returns the
    same list of dicts (mutated in place) for convenience.
    """
    if not chunks:
        return chunks

    m = model or get_model()
    texts = [PASSAGE_PROMPT + c["content"] for c in chunks]

    embeddings: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        for vec in m.embed(batch):
            embeddings.append(vec.tolist() if hasattr(vec, "tolist") else list(vec))

    if len(embeddings) != len(chunks):
        raise RuntimeError(
            f"embedding count mismatch: got {len(embeddings)} vectors for {len(chunks)} chunks"
        )

    for chunk, vec in zip(chunks, embeddings, strict=True):
        if len(vec) != EMBEDDING_DIM:
            raise RuntimeError(f"unexpected embedding dim {len(vec)} (expected {EMBEDDING_DIM})")
        chunk["embedding"] = vec

    logger.info("embedded_chunks", count=len(chunks))
    return chunks
