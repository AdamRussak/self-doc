"""Heading-aware markdown chunking.

Strategy:
  1. Split markdown on heading boundaries (`#`..`######`), tracking a
     breadcrumb `heading_path` like "Guide > Routing > Dynamic Routes".
  2. Within each heading section, window the content to `MIN_TOKENS`-
     `MAX_TOKENS` tokens with `OVERLAP_RATIO` (~15%) overlap, counted with the
     HuggingFace `tokenizers` BGE tokenizer (NOT tiktoken — wrong vocab for a
     BERT-family model).
  3. Fenced code blocks (``` ... ```) are never split across chunk
     boundaries. An oversize fenced block becomes its own chunk even if it
     exceeds MAX_TOKENS.

Output contract (per IMPLEMENTATION_PLAN.md T2 desc — depended on by the
embedder and by store.py in T4):

    {"url": str, "heading_path": str, "chunk_index": int, "content": str}
"""

from __future__ import annotations

import os
import re
import threading

from tokenizers import Tokenizer

from .logging_config import get_logger

MIN_TOKENS = 400
MAX_TOKENS = 600
OVERLAP_RATIO = 0.15
# Token counting follows the embedding model so chunk sizing matches the model
# that will actually embed the text (mismatched tokenizers can push a chunk
# past the model's context and silently truncate). Env-driven, same default as
# the embedder (config/models.yaml registry default).
TOKENIZER_MODEL_ID = os.environ.get("EMBEDDING_MODEL_NAME", "mixedbread-ai/mxbai-embed-large-v1")

logger = get_logger(component="chunker")

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^```")

_tokenizer_lock = threading.Lock()
_tokenizer: Tokenizer | None = None


def get_tokenizer() -> Tokenizer:
    """Lazily load (and cache) the embedding model's tokenizer.

    Downloads/loads TOKENIZER_MODEL_ID's tokenizer.json (via the tokenizers
    `from_pretrained` HF Hub path, which shares the local HF cache with
    FastEmbed's model download).
    """
    global _tokenizer
    with _tokenizer_lock:
        if _tokenizer is None:
            _tokenizer = Tokenizer.from_pretrained(TOKENIZER_MODEL_ID)
        return _tokenizer


def count_tokens(text: str, tokenizer: Tokenizer | None = None) -> int:
    tok = tokenizer or get_tokenizer()
    return len(tok.encode(text).ids)


class _Segment:
    """A contiguous run of lines belonging to one heading section, split into
    atomic units (either a single non-code line, or a whole fenced code
    block) so that code fences are never split."""

    __slots__ = ("heading_path", "units")

    def __init__(self, heading_path: str):
        self.heading_path = heading_path
        self.units: list[str] = []  # each unit is a renderable text blob


def _split_into_heading_segments(markdown: str) -> list[_Segment]:
    """Walk the markdown line-by-line, tracking the heading breadcrumb and
    grouping content into segments per heading. Fenced code blocks are kept
    intact as single atomic units within a segment."""
    lines = markdown.splitlines()
    breadcrumb: list[str] = []
    segments: list[_Segment] = [_Segment(heading_path="")]

    i = 0
    n = len(lines)
    buffer: list[str] = []

    def flush_buffer(seg: _Segment) -> None:
        if buffer:
            text = "\n".join(buffer).strip("\n")
            # Split into paragraph-sized atomic units (blank-line separated)
            # so the windower can pack/overlap at finer granularity than
            # "everything under this heading".
            for para in re.split(r"\n\s*\n", text):
                if para.strip():
                    seg.units.append(para.strip("\n"))
            buffer.clear()

    while i < n:
        line = lines[i]
        heading_match = _HEADING_RE.match(line)
        if heading_match:
            flush_buffer(segments[-1])
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            breadcrumb = breadcrumb[: level - 1]
            while len(breadcrumb) < level - 1:
                breadcrumb.append("")
            breadcrumb.append(title)
            heading_path = " > ".join(b for b in breadcrumb if b)
            segments.append(_Segment(heading_path=heading_path))
            i += 1
            continue

        if _FENCE_RE.match(line.strip()):
            flush_buffer(segments[-1])
            fence_lines = [line]
            i += 1
            while i < n and not _FENCE_RE.match(lines[i].strip()):
                fence_lines.append(lines[i])
                i += 1
            if i < n:
                fence_lines.append(lines[i])  # closing fence
                i += 1
            segments[-1].units.append("\n".join(fence_lines))
            continue

        buffer.append(line)
        i += 1

    flush_buffer(segments[-1])
    return [s for s in segments if s.units]


def _window_units(
    units: list[str],
    tokenizer: Tokenizer,
    min_tokens: int,
    max_tokens: int,
    overlap_ratio: float,
) -> list[str]:
    """Greedily pack atomic units into windows of min_tokens..max_tokens,
    with ~overlap_ratio overlap between consecutive windows. A single unit
    (e.g. a large fenced code block) larger than max_tokens becomes its own
    chunk unsplit."""
    unit_tokens = [count_tokens(u, tokenizer) for u in units]

    windows: list[list[int]] = []  # list of index-lists into `units`
    idx = 0
    n = len(units)
    while idx < n:
        window_indices: list[int] = []
        total = 0
        j = idx
        while j < n:
            t = unit_tokens[j]
            if window_indices and total + t > max_tokens:
                break
            window_indices.append(j)
            total += t
            j += 1
            if total >= min_tokens:
                break
        if not window_indices:
            # single oversize unit
            window_indices = [idx]
            j = idx + 1

        windows.append(window_indices)

        if j >= n:
            break

        # compute overlap: step back from j by ~overlap_ratio of this window's tokens
        window_token_total = sum(unit_tokens[k] for k in window_indices)
        overlap_budget = window_token_total * overlap_ratio
        back = 0
        consumed = 0
        k = len(window_indices) - 1
        while k >= 0 and consumed < overlap_budget:
            consumed += unit_tokens[window_indices[k]]
            back += 1
            k -= 1
        next_idx = j - back
        if next_idx <= idx:
            next_idx = j  # guarantee forward progress
        idx = next_idx

    return ["\n\n".join(units[i] for i in w) for w in windows]


def chunk_markdown(
    url: str,
    markdown: str,
    min_tokens: int = MIN_TOKENS,
    max_tokens: int = MAX_TOKENS,
    overlap_ratio: float = OVERLAP_RATIO,
    tokenizer: Tokenizer | None = None,
) -> list[dict]:
    """Chunk a page's markdown into `{url, heading_path, chunk_index, content}`
    dicts, split on heading boundaries then windowed to min_tokens..max_tokens
    with overlap. Never splits inside a fenced code block."""
    tok = tokenizer or get_tokenizer()
    log = logger.bind(url=url)

    segments = _split_into_heading_segments(markdown)
    chunks: list[dict] = []
    chunk_index = 0
    for seg in segments:
        windows = _window_units(seg.units, tok, min_tokens, max_tokens, overlap_ratio)
        for content in windows:
            content = content.strip()
            if not content:
                continue
            chunks.append(
                {
                    "url": url,
                    "heading_path": seg.heading_path or "",
                    "chunk_index": chunk_index,
                    "content": content,
                }
            )
            chunk_index += 1

    log.info("chunked", chunk_count=len(chunks))
    return chunks
