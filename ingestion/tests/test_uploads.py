import pytest

from app.uploads import (
    ALLOWED_SUFFIXES,
    MAX_ARCHIVE_MEMBERS,
    MAX_EXPANSION_RATIO,
    MAX_UNCOMPRESSED_BYTES,
    MAX_UPLOAD_BYTES,
    PARSERS,
    UploadedDoc,
    UploadError,
    normalize_rel_path,
    parse_upload,
    register_parser,
)

HTML_FIXTURE = """
<html><body>
<nav>Skip this nav content entirely please</nav>
<article>
<h1>Getting Started</h1>
<p>This is a fairly long paragraph of real documentation content that should
be extracted successfully by trafilatura because it exceeds the minimum
length threshold required for a page to be considered valid extraction
output rather than junk or empty boilerplate text.</p>
<p>Here is a second paragraph with even more useful detail about how to use
this fictional library, including examples and explanations that a reader
would find helpful when learning the API for the first time.</p>
</article>
<footer>Skip this footer content entirely please</footer>
</body></html>
"""


def test_constants_match_contract():
    assert MAX_UPLOAD_BYTES == 50 * 1024 * 1024
    assert MAX_ARCHIVE_MEMBERS == 500
    assert MAX_UNCOMPRESSED_BYTES == 200 * 1024 * 1024
    assert MAX_EXPANSION_RATIO == 100
    assert ALLOWED_SUFFIXES == {".md", ".markdown", ".txt", ".html", ".htm", ".pdf"}


def test_uploaded_doc_is_frozen():
    doc = UploadedDoc(rel_path="a.md", markdown="# hi")
    assert doc.rel_path == "a.md"
    assert doc.markdown == "# hi"
    with pytest.raises(Exception):
        doc.rel_path = "b.md"  # type: ignore[misc]


class TestNormalizeRelPath:
    def test_simple_relative_path(self):
        assert normalize_rel_path("docs/readme.md") == "docs/readme.md"

    def test_strips_leading_dot_segment(self):
        assert normalize_rel_path("./readme.md") == "readme.md"

    def test_bare_filename(self):
        assert normalize_rel_path("readme.md") == "readme.md"

    def test_rejects_absolute_path(self):
        with pytest.raises(UploadError):
            normalize_rel_path("/etc/passwd")

    def test_rejects_simple_traversal(self):
        with pytest.raises(UploadError):
            normalize_rel_path("../x")

    def test_rejects_combined_traversal(self):
        with pytest.raises(UploadError):
            normalize_rel_path("a/../../b")

    def test_rejects_backslash_separator(self):
        with pytest.raises(UploadError):
            normalize_rel_path("a\\b.md")

    def test_rejects_empty_path(self):
        with pytest.raises(UploadError):
            normalize_rel_path("")

    def test_error_message_has_no_path_leaked(self):
        # UploadError messages are shown to end users; must not echo the
        # attempted internal path/value back to the caller.
        try:
            normalize_rel_path("/etc/passwd")
        except UploadError as e:
            assert "/etc/passwd" not in str(e)
        else:
            pytest.fail("expected UploadError")


class TestParseUploadText:
    def test_markdown_passthrough(self):
        data = b"# Title\n\nSome body text."
        docs = parse_upload("notes.md", data)
        assert docs == [UploadedDoc(rel_path="notes.md", markdown="# Title\n\nSome body text.")]

    def test_markdown_alt_suffix(self):
        data = b"# Title"
        docs = parse_upload("notes.markdown", data)
        assert docs[0].markdown == "# Title"

    def test_txt_passthrough(self):
        data = b"plain text content"
        docs = parse_upload("notes.txt", data)
        assert docs == [UploadedDoc(rel_path="notes.txt", markdown="plain text content")]

    def test_decodes_invalid_utf8_with_replacement(self):
        data = b"valid text \xff\xfe more text"
        docs = parse_upload("notes.txt", data)
        assert len(docs) == 1
        assert "�" in docs[0].markdown

    def test_is_deterministic(self):
        data = "# Title\n\nRepeatable content.".encode("utf-8")
        first = parse_upload("notes.md", data)
        second = parse_upload("notes.md", data)
        assert first == second

    def test_suffix_matching_is_case_insensitive(self):
        data = b"# Title"
        docs = parse_upload("NOTES.MD", data)
        assert len(docs) == 1
        assert docs[0].markdown == "# Title"

    def test_rejects_traversal_filename(self):
        with pytest.raises(UploadError):
            parse_upload("../escape.md", b"content")


class TestParseUploadHtml:
    def test_html_round_trips_through_extract(self):
        docs = parse_upload("page.html", HTML_FIXTURE.encode("utf-8"))
        assert len(docs) == 1
        assert docs[0].rel_path == "page.html"
        assert "Getting Started" in docs[0].markdown
        assert len(docs[0].markdown) >= 200

    def test_htm_suffix_also_dispatches_to_html_parser(self):
        docs = parse_upload("page.htm", HTML_FIXTURE.encode("utf-8"))
        assert len(docs) == 1
        assert "Getting Started" in docs[0].markdown

    def test_is_deterministic(self):
        data = HTML_FIXTURE.encode("utf-8")
        first = parse_upload("page.html", data)
        second = parse_upload("page.html", data)
        assert first == second

    def test_too_short_html_raises_user_safe_error(self):
        data = b"<html><body><p>Too short.</p></body></html>"
        with pytest.raises(UploadError) as exc_info:
            parse_upload("page.html", data)
        # user-safe: no filesystem paths / internals in the message
        assert "page.html" not in str(exc_info.value)


class TestParseUploadUnsupported:
    def test_unknown_suffix_returns_empty_list(self):
        assert parse_upload("virus.exe", b"MZ\x00\x00") == []

    def test_no_suffix_returns_empty_list(self):
        assert parse_upload("README", b"content") == []

    def test_unregistered_allowed_suffix_returns_empty_list(self):
        # .pdf is in ALLOWED_SUFFIXES (future format) but has no parser
        # registered by this module; callers that implement PDF support
        # register a parser for it separately.
        if ".pdf" not in PARSERS:
            assert parse_upload("doc.pdf", b"%PDF-1.4") == []


class TestRegisterParser:
    def test_register_and_dispatch_custom_parser(self):
        calls = []

        def _custom(filename: str, data: bytes) -> list[UploadedDoc]:
            calls.append((filename, data))
            return [UploadedDoc(rel_path=filename, markdown="custom")]

        register_parser(".customext", _custom)
        try:
            docs = parse_upload("thing.customext", b"raw bytes")
            assert docs == [UploadedDoc(rel_path="thing.customext", markdown="custom")]
            assert calls == [("thing.customext", b"raw bytes")]
        finally:
            del PARSERS[".customext"]

    def test_register_parser_lowercases_suffix(self):
        def _custom(filename: str, data: bytes) -> list[UploadedDoc]:
            return [UploadedDoc(rel_path=filename, markdown="x")]

        register_parser(".UPPER", _custom)
        try:
            assert ".upper" in PARSERS
        finally:
            del PARSERS[".upper"]
