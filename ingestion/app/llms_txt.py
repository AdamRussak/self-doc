"""Discovery + parsing for the llmstxt.org `/llms.txt` / `/llms-full.txt`
convention.

`/llms.txt` is an index file (H1 title, optional blockquote summary, H2
sections listing markdown links to the real docs). `/llms-full.txt` is the
full concatenated documentation markdown for the site. We prefer the full
file when both are available, since it avoids a second round of crawling.

This module is PURE: no DB access, no logging side effects beyond the shared
structlog logger, and the httpx client is always caller-injected (never
constructed here) so it is trivially unit-testable with a fake/mock client.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from .logging_config import get_logger

USER_AGENT = "self-docs-crawler/0.1"

logger = get_logger(component="llms_txt")

_H1_RE = re.compile(r"^#\s+(.*)$")
_H2_RE = re.compile(r"^##\s+(.*)$")
_FENCE_RE = re.compile(r"^```")
_MD_LINK_RE = re.compile(r"^\[([^\]]*)\]\(([^)]+)\)$")
_SOURCE_LINE_RE = re.compile(r"^Source:\s*(https?://\S+)", re.IGNORECASE)
_SLUG_NONALNUM_RE = re.compile(r"[^a-z0-9]+")
# An llms.txt INDEX lists documentation pages as markdown link bullets, e.g.
#   - [Quickstart](https://x.io/docs/quickstart): get started fast
# Match such a bullet and capture its URL. Unlike `_MD_LINK_RE` this is NOT
# whole-line anchored: index bullets carry a leading `-`/`*` marker and an
# optional trailing `: description`.
_INDEX_LINK_ITEM_RE = re.compile(r"^\s*[-*]\s*\[[^\]]*\]\(([^)]+)\)")


def discover(
    client,
    base_url: str,
    *,
    prefer_full: bool = True,
) -> tuple[str, str] | None:
    """Try to fetch `/llms-full.txt` then `/llms.txt` (or the reverse order
    when `prefer_full=False`) at `base_url`'s origin.

    Returns `(fetched_url, text)` for the first response that is status 200
    with a non-empty body. There is NO size limit — the full file is fetched
    however large it is (llms-full.txt files are legitimately large). Returns
    `None` if neither candidate qualifies.

    NEVER raises: any httpx error, empty body, or non-200 status is treated as
    "skip this candidate" (and, if both candidates are exhausted, "skip this
    source" — return None) rather than propagating.
    """
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    candidates = [f"{origin}/llms-full.txt", f"{origin}/llms.txt"]
    if not prefer_full:
        candidates.reverse()

    log = logger.bind(base_url=base_url)

    for url in candidates:
        text = _fetch_body(client, url, log)
        if text is None:
            continue
        log.info("llms_txt_discovered", url=url, size=len(text))
        return url, text

    return None


def _fetch_body(client, url: str, log) -> str | None:
    """GET `url` and return its decoded text, or None to skip this candidate
    (non-200, empty, or any error — discovery is best-effort and never raises)."""
    try:
        resp = client.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
    except Exception as e:  # noqa: BLE001 - discovery is best-effort, never raises
        log.info("llms_txt_fetch_failed", url=url, error=str(e))
        return None
    if resp.status_code != 200:
        log.info("llms_txt_non_200", url=url, status=resp.status_code)
        return None
    text = resp.text
    if not text.strip():
        log.info("llms_txt_empty", url=url)
        return None
    return text


def _content_lines(text: str) -> list[str]:
    """Non-blank, non-heading lines outside fenced code blocks. Used to judge
    whether a file is an index (mostly link bullets) or full prose/code."""
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if _FENCE_RE.match(stripped):
            in_fence = not in_fence
            continue
        if in_fence or not stripped or stripped.startswith("#"):
            continue
        out.append(stripped)
    return out


def looks_like_index(text: str) -> bool:
    """True if `text` is an llms.txt INDEX (a list of links to the real docs)
    rather than `/llms-full.txt` full content.

    Per the llmstxt.org convention, `/llms.txt` is a curated index: an H1/H2
    outline whose body is markdown link bullets (`- [Title](url): note`), while
    `/llms-full.txt` is the concatenated documentation prose. This distinction
    matters because an index must be CRAWLED (fetch each linked page), never
    ingested verbatim — otherwise the corpus becomes a list of links with no
    actual documentation content.

    Heuristic: an index is dominated by link bullets. If at least half of the
    content lines (excluding headings/blank/code) are markdown link bullets AND
    there are several of them, it is an index. A full-content file has prose and
    code between its headings, so its link-bullet ratio is low.
    """
    content = _content_lines(text)
    if not content:
        return False
    link_bullets = sum(1 for line in content if _INDEX_LINK_ITEM_RE.match(line))
    # Require a real list (>=3 bullets) that is the majority of the content, so
    # a full-content page that merely happens to contain a couple of bullet
    # links is not misclassified as an index.
    return link_bullets >= 3 and link_bullets / len(content) >= 0.5


def parse_llms_index(text: str, base_url: str) -> list[str]:
    """Extract the documentation page URLs an llms.txt index links to.

    Returns absolute, order-preserving, de-duplicated URLs (relative links are
    resolved against `base_url`). Fragment-only or non-http(s) targets are
    dropped. Host/scope/private-address filtering is deliberately left to the
    crawler's per-URL `_visit` guards, mirroring how sitemap discovery hands
    raw candidate URLs to the same gate.
    """
    urls: list[str] = []
    seen: set[str] = set()
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if _FENCE_RE.match(stripped):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _INDEX_LINK_ITEM_RE.match(line)
        if not m:
            continue
        target = m.group(1).strip()
        if not target or target.startswith("#"):
            continue
        absolute = urljoin(base_url, target)
        if not absolute.lower().startswith(("http://", "https://")):
            continue
        if absolute not in seen:
            seen.add(absolute)
            urls.append(absolute)
    return urls


def _slugify(title: str) -> str:
    """Deterministic slug: lowercase, runs of non-alphanumeric collapsed to a
    single '-', leading/trailing '-' stripped."""
    slug = _SLUG_NONALNUM_RE.sub("-", title.lower()).strip("-")
    return slug


def _strip_title_markup(title: str) -> str:
    """Strip markdown-link syntax from a heading title, returning the plain
    link text if the whole title is a single markdown link (`[Title](url)`),
    else the title unchanged (already plain)."""
    m = _MD_LINK_RE.match(title.strip())
    if m:
        return m.group(1).strip()
    return title.strip()


def _section_url(title: str, body_lines: list[str], source_url: str) -> str:
    """Derive a stable, deterministic URL for a section.

    Priority:
      1. Heading is a markdown link `[Title](URL)` -> use URL.
      2. Section body contains a `Source: <http(s) url>` line -> use that URL.
      3. Fallback: `{source_url}#{slug(title)}`.
    """
    link_match = _MD_LINK_RE.match(title.strip())
    if link_match:
        return link_match.group(2).strip()

    for line in body_lines:
        src_match = _SOURCE_LINE_RE.match(line.strip())
        if src_match:
            return src_match.group(1).strip()

    return f"{source_url}#{_slugify(_strip_title_markup(title))}"


def _find_heading_lines(lines: list[str], heading_re: re.Pattern) -> list[int]:
    """Indices of lines matching `heading_re`, skipping any line inside a
    fenced code block (``` ... ```) so a `#`/`##` inside a fence is never
    treated as a heading."""
    indices: list[int] = []
    in_fence = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if _FENCE_RE.match(stripped):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if heading_re.match(line):
            indices.append(i)
    return indices


def split_llms_full(text: str, source_url: str) -> list[dict]:
    """Split an `/llms-full.txt` document into per-section dicts.

    Splits on top-level headings: H1 (`# `) is preferred; if fewer than 2 H1
    sections exist, falls back to H2 (`## `). If still only a single section
    results, the whole text is returned as one section. Never splits inside a
    fenced code block.

    Each section is `{"url": str, "markdown": str, "heading_path": str}`,
    where `heading_path` is the plain section title (leading `#`s and any
    markdown-link syntax stripped) and `url` is a stable, deterministic
    per-section URL (see `_section_url`).

    Any non-empty preamble before the first heading is included as its own
    section with `heading_path=""` and `url=f"{source_url}#preamble"`.
    """
    lines = text.splitlines()

    h1_indices = _find_heading_lines(lines, _H1_RE)
    if len(h1_indices) >= 2:
        heading_indices = h1_indices
        heading_re = _H1_RE
    else:
        h2_indices = _find_heading_lines(lines, _H2_RE)
        if len(h2_indices) >= 2:
            heading_indices = h2_indices
            heading_re = _H2_RE
        else:
            heading_indices = []
            heading_re = None

    sections: list[dict] = []

    if not heading_indices:
        stripped = text.strip()
        if stripped:
            sections.append(
                {
                    "url": f"{source_url}#preamble",
                    "markdown": text,
                    "heading_path": "",
                }
            )
        return sections

    # Preamble: everything before the first heading.
    preamble_lines = lines[: heading_indices[0]]
    preamble_text = "\n".join(preamble_lines).strip()
    if preamble_text:
        sections.append(
            {
                "url": f"{source_url}#preamble",
                "markdown": preamble_text,
                "heading_path": "",
            }
        )

    for pos, start in enumerate(heading_indices):
        end = heading_indices[pos + 1] if pos + 1 < len(heading_indices) else len(lines)
        heading_line = lines[start]
        match = heading_re.match(heading_line)
        raw_title = match.group(1).strip() if match else heading_line.lstrip("#").strip()
        title = _strip_title_markup(raw_title)

        section_lines = lines[start:end]
        body_lines = lines[start + 1 : end]
        markdown = "\n".join(section_lines).strip()

        url = _section_url(raw_title, body_lines, source_url)

        sections.append(
            {
                "url": url,
                "markdown": markdown,
                "heading_path": title,
            }
        )

    return sections
