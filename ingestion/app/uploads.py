"""Upload parsing: raw file bytes -> parsed markdown docs (in-memory only).

Pure-function core for the upload ingestion path (admin UI / CLI / MCP
tool). No DB access, no filesystem writes, no network calls: every function
here operates on already-read bytes and returns in-memory dataclasses.
Callers are responsible for persisting the parsed markdown via the existing
chunker/embedder pipeline; raw uploaded bytes are never written to disk or
the database.

This module is a shared contract: PDF parsing (T4) and zip-bundle expansion
(T5) register additional parsers into `PARSERS` via `register_parser` and
reuse `UploadedDoc`, `UploadError`, and `normalize_rel_path` from here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from .extract import extract

# Hard limits on what an upload may contain. Enforced by callers (API layer /
# archive expansion) before or while invoking the parsers in this module;
# the parsers themselves do not re-check them.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 500
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_EXPANSION_RATIO = 100

ALLOWED_SUFFIXES = {".md", ".markdown", ".txt", ".html", ".htm", ".pdf"}


class UploadError(Exception):
    """Raised when an upload cannot be parsed.

    The exception message is shown directly to end users (admin UI, CLI,
    MCP tool output) - it must never contain filesystem paths, stack
    traces, or other internal details.
    """


@dataclass(frozen=True)
class UploadedDoc:
    rel_path: str
    markdown: str


PARSERS: dict[str, Callable[[str, bytes], list[UploadedDoc]]] = {}


def register_parser(suffix: str, fn: Callable[[str, bytes], list[UploadedDoc]]) -> None:
    """Register a parser for a lowercase file suffix (e.g. '.pdf').

    `fn` takes (filename, data) and returns a list of UploadedDoc. Later
    registrations for the same suffix silently replace earlier ones.
    """
    PARSERS[suffix.lower()] = fn


def normalize_rel_path(path: str) -> str:
    """Normalize an upload-relative path to a safe POSIX-style relative path.

    Raises UploadError if `path` is empty, absolute, uses a backslash path
    separator, or contains a '..' segment (directly or via combination,
    e.g. 'a/../../b').
    """
    if not path or not path.strip():
        raise UploadError("Uploaded file has an empty or invalid path.")
    if "\\" in path:
        raise UploadError("Uploaded file path uses an unsupported path separator.")

    pure = PurePosixPath(path)
    if pure.is_absolute():
        raise UploadError("Uploaded file path must be relative.")

    parts = [p for p in pure.parts if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UploadError("Uploaded file path may not contain '..' segments.")
    if not parts:
        raise UploadError("Uploaded file has an empty or invalid path.")

    return "/".join(parts)


def _parse_text(filename: str, data: bytes) -> list[UploadedDoc]:
    """Markdown/plain-text passthrough: decode and use unchanged."""
    text = data.decode("utf-8", errors="replace")
    rel_path = normalize_rel_path(filename)
    return [UploadedDoc(rel_path=rel_path, markdown=text)]


def _parse_html(filename: str, data: bytes) -> list[UploadedDoc]:
    """HTML upload: reuse extract.py's trafilatura/BS4 pipeline as-is."""
    html = data.decode("utf-8", errors="replace")
    rel_path = normalize_rel_path(filename)
    result = extract(f"upload://{rel_path}", html)
    if result.status != "ok" or not result.markdown:
        raise UploadError("Could not extract readable content from the uploaded HTML file.")
    return [UploadedDoc(rel_path=rel_path, markdown=result.markdown)]


register_parser(".md", _parse_text)
register_parser(".markdown", _parse_text)
register_parser(".txt", _parse_text)
register_parser(".html", _parse_html)
register_parser(".htm", _parse_html)


def parse_upload(filename: str, data: bytes) -> list[UploadedDoc]:
    """Parse raw uploaded file bytes into zero or more UploadedDoc entries.

    Dispatches on the lowercased suffix of `filename` via the `PARSERS`
    registry. Unknown/unsupported suffixes (including suffixes with no
    parser registered yet, e.g. archive suffixes handled elsewhere) return
    an empty list rather than raising.
    """
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    parser = PARSERS.get(suffix)
    if parser is None:
        return []
    return parser(filename, data)
