"""Main-content extraction: HTML → markdown.

Primary path is `trafilatura` (strips nav/sidebar/footer boilerplate far
better than hand-rolled CSS selectors); falls back to a BeautifulSoup
plain-text extraction if trafilatura yields nothing. A minimum
extracted-length sanity check guards against near-empty extractions (e.g. a
JS-rendered shell page) — pages below the threshold are reported as
skipped/failed rather than silently indexed as near-empty chunks.
"""

from __future__ import annotations

from dataclasses import dataclass

import trafilatura
from bs4 import BeautifulSoup

from .logging_config import get_logger

MIN_EXTRACTED_LENGTH = 200

logger = get_logger(component="extract")


@dataclass
class ExtractionResult:
    url: str
    markdown: str | None
    status: str  # "ok" | "skipped" | "failed"
    reason: str | None = None


def _bs4_fallback_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def extract(url: str, html: str, min_length: int = MIN_EXTRACTED_LENGTH) -> ExtractionResult:
    """Extract main content as markdown for a single fetched page.

    Tries trafilatura first (markdown output), falls back to a BS4 text
    extraction. Rejects (status="skipped") extractions shorter than
    `min_length` characters.
    """
    log = logger.bind(url=url)

    markdown = None
    try:
        markdown = trafilatura.extract(
            html,
            output_format="markdown",
            include_links=False,
            include_images=False,
            favor_precision=True,
        )
    except Exception as e:  # noqa: BLE001 - trafilatura can raise on malformed HTML
        log.info("trafilatura_failed", error=str(e))
        markdown = None

    used_fallback = False
    current_len = len(markdown.strip()) if markdown else 0
    if current_len < min_length:
        fallback_text = _bs4_fallback_text(html)
        if len(fallback_text.strip()) > current_len:
            markdown = fallback_text
            used_fallback = True

    if not markdown or len(markdown.strip()) < min_length:
        log.info("extraction_too_short", length=len(markdown.strip()) if markdown else 0, used_fallback=used_fallback)
        return ExtractionResult(url=url, markdown=None, status="skipped", reason="extracted content below minimum length")

    log.info("extraction_ok", length=len(markdown.strip()), used_fallback=used_fallback)
    return ExtractionResult(url=url, markdown=markdown.strip(), status="ok")
