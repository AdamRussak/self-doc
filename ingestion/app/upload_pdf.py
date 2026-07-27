"""PDF upload parsing: raw PDF bytes -> per-page extracted text as markdown.

Registers a `.pdf` parser into `uploads.PARSERS` (see `uploads.py` for the
shared `UploadedDoc`/`UploadError`/`register_parser` contract this module
plugs into).

`pypdf` is imported lazily *inside* `_parse_pdf`, not at module import time,
so that `import ingestion.app.upload_pdf` (and therefore
`import ingestion.app.uploads`, which does not depend on this module at all)
never fails just because `pypdf` happens to be missing from the environment.
Failure is deferred to the moment a `.pdf` file is actually uploaded, at
which point it surfaces as a normal user-safe `UploadError` rather than an
import-time crash of the whole ingestion service.

pypdf API used here (pypdf 6.14.2, confirmed by reading the installed
package's own docstrings/source via
`python -c "import pypdf; help(pypdf.PdfReader)"` and
`pypdf.errors` — not present in the self-docs documentation index, so we do
not rely on memory of this library):

- `pypdf.PdfReader(stream)` accepts any binary file-like object, so an
  in-memory `io.BytesIO(data)` is used directly; nothing is written to disk.
- `PdfReader.is_encrypted` is a read-only property that is `True` for
  encrypted PDFs *before* any page is touched, letting us reject encrypted
  files up front without ever calling `decrypt()` or prompting for a
  password (confirmed: accessing `.pages`/`.extract_text()` on an encrypted,
  non-decrypted reader raises `pypdf.errors.FileNotDecryptedError`, which we
  never want to hit).
- `PdfReader.pages` is a lazy sequence; `len(reader.pages)` gives the page
  count without extracting any text, which is what the page-count cap below
  checks before doing any extraction work.
- `PageObject.extract_text()` returns the page's text as a `str` (possibly
  empty for image-only/scanned pages); pypdf's own docs warn text order is
  not guaranteed to be reading order, which is fine for our purposes since
  we chunk/embed the plain text, not renders.

PDFs carry no markdown heading structure. The per-page text extracted below
is joined with blank lines and handed to
`ingestion/app/chunker.py:chunk_markdown` as-is (no `#`/`##`/etc. headings
are synthesized), so PDF-derived chunks will always have an empty/None
`heading_path`. This is not a bug — it is cosmetically identical to the
existing `pgvector-readme` source already live in the index, which also
indexes with an empty `heading_path`.
"""

from __future__ import annotations

import io

from .extract import MIN_EXTRACTED_LENGTH
from .uploads import UploadedDoc, UploadError, normalize_rel_path, register_parser

# Defends against pathological/adversarial PDFs (e.g. a page tree crafted to
# contain millions of pages) driving unbounded extraction work. Real-world
# documentation PDFs are nowhere near this size; beyond it we reject rather
# than silently truncate, so operators get an explicit signal instead of a
# partially-indexed document.
MAX_PDF_PAGES = 2000


def _parse_pdf(filename: str, data: bytes) -> list[UploadedDoc]:
    """Extract text from an uploaded PDF's pages into a single markdown doc.

    Raises UploadError (user-safe message, no internals) for: encrypted
    PDFs, unreadable/corrupt PDFs, PDFs over MAX_PDF_PAGES pages, and PDFs
    whose extracted text is too short to be useful (e.g. scanned/image-only
    pages with no text layer).
    """
    try:
        import pypdf
    except ImportError as e:
        raise UploadError("PDF support is not available on this server.") from e

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
    except Exception as e:
        raise UploadError("Could not read the uploaded PDF file.") from e

    # Check encryption before touching pages at all: no decrypt() call, no
    # password prompt, no risk of hanging on a protected file.
    if reader.is_encrypted:
        raise UploadError("Encrypted PDFs are not supported.")

    try:
        num_pages = len(reader.pages)
    except Exception as e:  # noqa: BLE001 - pypdf raises assorted internal errors on malformed files
        raise UploadError("Could not read the uploaded PDF file.") from e

    if num_pages > MAX_PDF_PAGES:
        raise UploadError(f"PDF has too many pages (max {MAX_PDF_PAGES}).")

    page_texts: list[str] = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - a single malformed page shouldn't fail the whole document
            text = ""
        page_texts.append(text.strip())

    markdown = "\n\n".join(page_texts).strip()

    if len(markdown) < MIN_EXTRACTED_LENGTH:
        raise UploadError("no extractable text (scanned PDF?)")

    rel_path = normalize_rel_path(filename)
    return [UploadedDoc(rel_path=rel_path, markdown=markdown)]


register_parser(".pdf", _parse_pdf)
