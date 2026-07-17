import pytest

from app.chunker import chunk_markdown, count_tokens, get_tokenizer

TOKENIZER = get_tokenizer()


def make_paragraph(words: int, seed: str = "word") -> str:
    return " ".join(f"{seed}{i}" for i in range(words))


def test_heading_path_breadcrumb():
    markdown = f"""# Guide

## Routing

### Dynamic Routes

{make_paragraph(500)}
"""
    chunks = chunk_markdown("https://example.com/docs", markdown, tokenizer=TOKENIZER)
    assert len(chunks) >= 1
    assert chunks[0]["heading_path"] == "Guide > Routing > Dynamic Routes"
    assert chunks[0]["url"] == "https://example.com/docs"
    assert chunks[0]["chunk_index"] == 0


def test_chunk_contract_keys():
    markdown = f"# Title\n\n{make_paragraph(450)}\n"
    chunks = chunk_markdown("https://example.com/x", markdown, tokenizer=TOKENIZER)
    for c in chunks:
        assert set(c.keys()) == {"url", "heading_path", "chunk_index", "content"}


def test_window_token_bounds_roughly_respected():
    # A long single section well beyond max_tokens forces multiple windows.
    markdown = "# Section\n\n" + "\n\n".join(make_paragraph(80, seed=f"p{i}_") for i in range(20))
    chunks = chunk_markdown("https://example.com/long", markdown, tokenizer=TOKENIZER)
    assert len(chunks) > 1
    for c in chunks[:-1]:  # last chunk may be a short remainder
        tokens = count_tokens(c["content"], TOKENIZER)
        assert tokens <= 700  # allow slack for atomic-unit granularity


def test_overlap_between_consecutive_windows():
    markdown = "# Section\n\n" + "\n\n".join(make_paragraph(60, seed=f"p{i}_") for i in range(20))
    chunks = chunk_markdown("https://example.com/long2", markdown, tokenizer=TOKENIZER)
    assert len(chunks) > 1
    # Consecutive chunks should share at least some content (overlap).
    first_tail = chunks[0]["content"][-200:]
    second_head = chunks[1]["content"]
    shared = any(line in second_head for line in first_tail.split("\n") if line.strip())
    assert shared


def test_code_fence_never_split():
    long_code = "\n".join(f"line_{i} = {i}" for i in range(400))
    markdown = f"""# API

Some intro text before the code block that provides context and explains
what the following example demonstrates in reasonable detail for readers.

```python
{long_code}
```

Some text after.
"""
    chunks = chunk_markdown("https://example.com/code", markdown, tokenizer=TOKENIZER)
    # The fenced block must appear whole in exactly one chunk, never truncated mid-fence.
    fence_chunks = [c for c in chunks if "```python" in c["content"]]
    assert len(fence_chunks) == 1
    assert fence_chunks[0]["content"].count("```") == 2
    assert "line_399 = 399" in fence_chunks[0]["content"]


def test_chunk_index_sequential_per_page():
    markdown = f"""# A

{make_paragraph(450)}

## B

{make_paragraph(450, seed="q")}
"""
    chunks = chunk_markdown("https://example.com/idx", markdown, tokenizer=TOKENIZER)
    indices = [c["chunk_index"] for c in chunks]
    assert indices == list(range(len(chunks)))
