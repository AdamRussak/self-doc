"""Tests for app/upload_pdf.py's `.pdf` parser.

Fixture PDFs are generated in-process rather than committed as binary
files, so the test suite stays self-contained and reproducible without
depending on any external PDF-generation tool. The "text" and
"no-extractable-text" fixtures are hand-rolled per the PDF 1.4 spec
(minimal objects + xref table); the "encrypted" fixture is built with
pypdf's own `PdfWriter.encrypt` since exercising the real encrypt/decrypt
path is the point of that test.
"""

from __future__ import annotations

import io
import pathlib
import subprocess
import sys

import pytest

import app.upload_pdf  # noqa: F401 - side effect: registers the .pdf parser into PARSERS
from app.extract import MIN_EXTRACTED_LENGTH
from app.uploads import PARSERS, UploadedDoc, UploadError, parse_upload


def _write_pdf_objects(objects: list[bytes]) -> bytes:
    """Assemble a minimal single-xref PDF from a list of already-formatted
    indirect-object bodies (1 0 obj .. endobj is added here)."""
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets = [0]
    for idx, obj in enumerate(objects, start=1):
        offsets.append(buf.tell())
        buf.write(f"{idx} 0 obj\n".encode())
        buf.write(obj)
        buf.write(b"\nendobj\n")
    xref_offset = buf.tell()
    n_obj = len(objects) + 1
    buf.write(f"xref\n0 {n_obj}\n".encode())
    buf.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        buf.write(f"{off:010d} 00000 n \n".encode())
    buf.write(f"trailer\n<< /Size {n_obj} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode())
    return buf.getvalue()


def make_text_pdf(pages_lines: list[list[str]]) -> bytes:
    """Build a PDF whose pages contain real Tj text-showing operators
    against the standard (unembedded) Helvetica font, so pypdf's
    extract_text() returns the lines back out."""

    def esc(s: str) -> str:
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    n_pages = len(pages_lines)
    page_obj_start = 3
    content_obj_start = page_obj_start + n_pages
    font_obj_num = content_obj_start + n_pages

    objects: list[bytes] = []
    kids = " ".join(f"{page_obj_start + i} 0 R" for i in range(n_pages))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())

    for i in range(n_pages):
        content_num = content_obj_start + i
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 {font_obj_num} 0 R >> >> "
                f"/MediaBox [0 0 612 792] /Contents {content_num} 0 R >>"
            ).encode()
        )

    for lines in pages_lines:
        parts = ["BT", "/F1 12 Tf", "72 750 Td"]
        for i, line in enumerate(lines):
            if i:
                parts.append("0 -14 Td")
            parts.append(f"({esc(line)}) Tj")
        parts.append("ET")
        stream = "\n".join(parts).encode()
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")

    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    return _write_pdf_objects(objects)


def make_no_text_pdf() -> bytes:
    """A single page with graphical content (a filled rectangle) but no
    text-showing operators at all: simulates a scanned/image-only page for
    testing that extract_text() yields nothing usable."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << >> /MediaBox [0 0 200 200] /Contents 4 0 R >>",
    ]
    stream = b"0.5 g 20 20 160 160 re f"
    objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    return _write_pdf_objects(objects)


def make_encrypted_pdf(password: str = "secret123") -> bytes:
    import pypdf

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt(user_password=password)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


LONG_LINE = "This line contains real extractable text used to exceed the minimum length threshold for a valid PDF page."


class TestParsePdfText:
    def test_extracts_text_over_minimum_length(self):
        pdf_bytes = make_text_pdf([[LONG_LINE] * 3, [LONG_LINE] * 3])
        docs = parse_upload("manual.pdf", pdf_bytes)
        assert isinstance(docs, list)
        assert len(docs) == 1
        assert isinstance(docs[0], UploadedDoc)
        assert len(docs[0].markdown) > MIN_EXTRACTED_LENGTH
        assert "real extractable text" in docs[0].markdown

    def test_rel_path_matches_filename(self):
        pdf_bytes = make_text_pdf([[LONG_LINE] * 3])
        docs = parse_upload("docs/manual.pdf", pdf_bytes)
        assert docs[0].rel_path == "docs/manual.pdf"

    def test_pages_joined_with_blank_line(self):
        pdf_bytes = make_text_pdf([[LONG_LINE, "PAGE-ONE-MARKER"], [LONG_LINE, "PAGE-TWO-MARKER"]])
        docs = parse_upload("two-pages.pdf", pdf_bytes)
        markdown = docs[0].markdown
        assert "PAGE-ONE-MARKER" in markdown
        assert "PAGE-TWO-MARKER" in markdown
        # Pages are joined with a blank line, not concatenated directly.
        one_idx = markdown.index("PAGE-ONE-MARKER")
        two_idx = markdown.index("PAGE-TWO-MARKER")
        between = markdown[one_idx:two_idx]
        assert "\n\n" in between

    def test_min_length_check_runs_on_joined_result(self):
        # Each page alone is short, but together they clear the threshold
        # only when actually joined and measured as one document -- proves
        # the check runs on the joined text, not per-page.
        pdf_bytes = make_text_pdf([[LONG_LINE], [LONG_LINE]])
        docs = parse_upload("joined.pdf", pdf_bytes)
        assert len(docs[0].markdown) > MIN_EXTRACTED_LENGTH


class TestParsePdfScanned:
    def test_scanned_pdf_raises_upload_error(self):
        pdf_bytes = make_no_text_pdf()
        with pytest.raises(UploadError) as exc_info:
            parse_upload("scanned.pdf", pdf_bytes)
        assert "no extractable text" in str(exc_info.value)

    def test_too_short_text_raises_same_error(self):
        # Two short pages joined together still fall under
        # MIN_EXTRACTED_LENGTH.
        pdf_bytes = make_text_pdf([["short"], ["also short"]])
        with pytest.raises(UploadError) as exc_info:
            parse_upload("tiny.pdf", pdf_bytes)
        assert "no extractable text" in str(exc_info.value)


class TestParsePdfEncrypted:
    def test_encrypted_pdf_raises_upload_error_no_hang(self):
        pdf_bytes = make_encrypted_pdf()
        with pytest.raises(UploadError) as exc_info:
            parse_upload("protected.pdf", pdf_bytes)
        assert "Encrypted" in str(exc_info.value)

    def test_encrypted_error_message_is_user_safe(self):
        pdf_bytes = make_encrypted_pdf()
        with pytest.raises(UploadError) as exc_info:
            parse_upload("protected.pdf", pdf_bytes)
        assert "protected.pdf" not in str(exc_info.value)
        assert "Traceback" not in str(exc_info.value)


class TestParsePdfPageCap:
    def test_over_page_cap_raises_upload_error(self):
        import pypdf
        from app.upload_pdf import MAX_PDF_PAGES

        writer = pypdf.PdfWriter()
        for _ in range(MAX_PDF_PAGES + 1):
            writer.add_blank_page(width=50, height=50)
        buf = io.BytesIO()
        writer.write(buf)

        with pytest.raises(UploadError) as exc_info:
            parse_upload("huge.pdf", buf.getvalue())
        assert "too many pages" in str(exc_info.value)


class TestParsePdfCorrupt:
    def test_garbage_bytes_raise_upload_error(self):
        with pytest.raises(UploadError):
            parse_upload("broken.pdf", b"not a real pdf file at all")


class TestRegistration:
    def test_pdf_parser_is_registered(self):
        assert ".pdf" in PARSERS

    def test_dispatches_through_parse_upload(self):
        pdf_bytes = make_text_pdf([[LONG_LINE] * 3])
        docs = parse_upload("thing.pdf", pdf_bytes)
        assert docs[0].markdown


class TestLazyImportIsolation:
    def test_uploads_import_succeeds_without_pypdf(self, monkeypatch):
        # sys.modules[name] = None makes `import pypdf` raise ImportError,
        # simulating pypdf being absent from the environment. This exercises
        # the *already-imported* app.upload_pdf module's lazy import inside
        # _parse_pdf -- proving the ImportError is caught and turned into a
        # user-safe UploadError at call time, not at module-import time.
        monkeypatch.setitem(sys.modules, "pypdf", None)

        from app.upload_pdf import _parse_pdf

        with pytest.raises(UploadError) as exc_info:
            _parse_pdf("x.pdf", b"whatever")
        assert "not available" in str(exc_info.value)

    def test_fresh_process_imports_succeed_without_pypdf(self):
        # Full-fidelity proof, run in a brand-new interpreter (no leftover
        # sys.modules state from this test session): block `import pypdf`
        # via sys.modules[None] *before* anything else is imported, then
        # import both app.uploads and app.upload_pdf fresh and confirm
        # neither raises and the .pdf parser is still registered.
        script = (
            "import sys; sys.modules['pypdf'] = None\n"
            "import app.uploads as uploads\n"
            "import app.upload_pdf as upload_pdf\n"
            "assert '.pdf' in uploads.PARSERS\n"
            "try:\n"
            "    upload_pdf._parse_pdf('x.pdf', b'whatever')\n"
            "except uploads.UploadError as e:\n"
            "    assert 'not available' in str(e)\n"
            "else:\n"
            "    raise SystemExit('expected UploadError')\n"
            "print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(pathlib.Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "OK" in result.stdout
