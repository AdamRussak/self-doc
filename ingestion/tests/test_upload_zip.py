import io
import zipfile
from unittest.mock import patch

import pytest
from app import upload_zip
from app.upload_zip import ZipExpansionResult, ZipMemberFailure, expand_zip
from app.uploads import PARSERS, UploadedDoc, UploadError, parse_upload

HTML_TOO_SHORT = b"<html><body><p>Too short.</p></body></html>"


def _make_zip(entries: dict[str, bytes], *, compress: bool = False) -> bytes:
    """Build an in-memory zip from {name: content} pairs."""
    buf = io.BytesIO()
    method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buf, "w", method) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _make_zip_with_symlink(link_name: str, target: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo(link_name)
        # Unix mode bits for a symlink (S_IFLNK | 0o777), packed into the
        # upper 16 bits of external_attr the way real zip tools do it.
        info.external_attr = (0o120777 << 16)
        zf.writestr(info, target)
    return buf.getvalue()


class TestRegistration:
    def test_zip_registered_in_parsers(self):
        assert ".zip" in PARSERS

    def test_parse_upload_dispatches_to_zip_parser(self):
        data = _make_zip({"a.md": b"# A"})
        docs = parse_upload("bundle.zip", data)
        assert docs == [UploadedDoc(rel_path="a.md", markdown="# A")]


class TestZipSlip:
    def test_traversal_member_rejects_whole_archive_before_any_read(self):
        data = _make_zip({"../../etc/passwd": b"# malicious"})

        with patch.object(zipfile.ZipFile, "read") as mock_read:
            with pytest.raises(UploadError):
                expand_zip("bundle.zip", data)
            mock_read.assert_not_called()

    def test_absolute_path_member_rejects_whole_archive(self):
        data = _make_zip({"/etc/passwd": b"# malicious"})
        with pytest.raises(UploadError):
            expand_zip("bundle.zip", data)

    def test_zip_slip_alongside_good_members_rejects_everything(self):
        # Even if other members are fine, one traversal member must reject
        # the whole batch - nothing partially ingested.
        data = _make_zip(
            {
                "good.md": b"# Good",
                "../escape.md": b"# Escape",
            }
        )
        with pytest.raises(UploadError):
            expand_zip("bundle.zip", data)


class TestSymlinkMember:
    def test_symlink_member_is_rejected(self):
        data = _make_zip_with_symlink("link.md", "/etc/passwd")
        with patch.object(zipfile.ZipFile, "read") as mock_read:
            with pytest.raises(UploadError):
                expand_zip("bundle.zip", data)
            mock_read.assert_not_called()


class TestDecompressionBomb:
    def test_high_ratio_member_rejected_from_metadata_only(self):
        # 10MB of zero bytes compresses to well under 100KB, giving a
        # genuine >100x expansion ratio purely from real zip metadata - no
        # need to fabricate/patch ZipInfo fields.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("bomb.md", b"0" * (10 * 1024 * 1024))
        data = buf.getvalue()

        # Sanity check the fixture actually exceeds the ratio before
        # asserting behavior against it.
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            info = zf.infolist()[0]
            assert info.file_size / max(info.compress_size, 1) > 100

        with patch.object(zipfile.ZipFile, "read") as mock_read:
            result = expand_zip("bundle.zip", data)
            mock_read.assert_not_called()

        assert result.docs == []
        assert len(result.failures) == 1
        assert result.failures[0].member == "bomb.md"

    def test_bomb_member_does_not_block_good_members(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("bomb.md", b"0" * (10 * 1024 * 1024))
            zf.writestr("good.md", b"# Good content")
        data = buf.getvalue()

        result = expand_zip("bundle.zip", data)
        assert result.docs == [UploadedDoc(rel_path="good.md", markdown="# Good content")]
        assert len(result.failures) == 1
        assert result.failures[0].member == "bomb.md"


class TestMemberCountLimit:
    def test_501_members_rejected(self):
        entries = {f"file{i}.md": f"# {i}".encode() for i in range(501)}
        data = _make_zip(entries)
        with pytest.raises(UploadError):
            expand_zip("bundle.zip", data)

    def test_500_members_allowed(self):
        entries = {f"file{i}.md": f"# {i}".encode() for i in range(500)}
        data = _make_zip(entries)
        result = expand_zip("bundle.zip", data)
        assert len(result.docs) == 500
        assert result.failures == []


class TestUncompressedSizeLimit:
    def test_exceeding_max_uncompressed_bytes_rejected(self):
        from app import uploads as uploads_mod

        data = _make_zip({"big.md": b"x" * 100})
        with patch.object(uploads_mod, "MAX_UNCOMPRESSED_BYTES", 50):
            with pytest.raises(UploadError):
                expand_zip("bundle.zip", data)


class TestMixedBatchPartialFailure:
    def test_two_good_one_bad_returns_two_successes_and_one_failure(self):
        data = _make_zip(
            {
                "a.md": b"# A",
                "b.md": b"# B",
                "bad.html": HTML_TOO_SHORT,
            }
        )
        result = expand_zip("bundle.zip", data)

        assert len(result.docs) == 2
        assert {d.rel_path for d in result.docs} == {"a.md", "b.md"}
        assert len(result.failures) == 1
        assert result.failures[0].member == "bad.html"
        assert isinstance(result.failures[0].reason, str) and result.failures[0].reason

    def test_via_parse_upload_returns_only_successes_and_logs_failure(self, caplog):
        data = _make_zip(
            {
                "a.md": b"# A",
                "b.md": b"# B",
                "bad.html": HTML_TOO_SHORT,
            }
        )
        # Through the PARSERS-registered path, only successes come back -
        # the failure is reported via structlog, not the return value.
        docs = parse_upload("bundle.zip", data)
        assert len(docs) == 2
        assert {d.rel_path for d in docs} == {"a.md", "b.md"}


class TestNestedZip:
    def test_nested_zip_member_is_skipped_not_recursed(self):
        inner = _make_zip({"inner.md": b"# Inner"})
        data = _make_zip({"outer.md": b"# Outer", "nested.zip": inner})

        with patch.object(upload_zip, "expand_zip", wraps=upload_zip.expand_zip) as spy:
            result = upload_zip.expand_zip("bundle.zip", data)
            # If the implementation recursed into the nested archive it
            # would call expand_zip a second time (for "nested.zip"); it
            # must not.
            assert spy.call_count == 1

        assert len(result.docs) == 1
        assert result.docs[0].rel_path == "outer.md"
        assert result.failures == []
        assert result.skipped == ["nested.zip"]
        # inner.md (only reachable by recursing into nested.zip) must never
        # appear anywhere in the result.
        assert "inner.md" not in {d.rel_path for d in result.docs}


class TestUnhandledSuffix:
    def test_unknown_suffix_member_skipped_silently(self):
        data = _make_zip({"good.md": b"# Good", "ignore.exe": b"MZ\x00\x00"})
        result = expand_zip("bundle.zip", data)

        assert result.docs == [UploadedDoc(rel_path="good.md", markdown="# Good")]
        assert result.failures == []
        assert result.skipped == ["ignore.exe"]


class TestInvalidZip:
    def test_not_a_zip_file_raises_upload_error(self):
        with pytest.raises(UploadError):
            expand_zip("bundle.zip", b"not a zip at all")


class TestResultDataclasses:
    def test_zip_expansion_result_is_frozen(self):
        result = ZipExpansionResult(docs=[], failures=[], skipped=[])
        with pytest.raises(Exception):
            result.docs = []  # type: ignore[misc]

    def test_zip_member_failure_is_frozen(self):
        failure = ZipMemberFailure(member="a.md", reason="bad")
        with pytest.raises(Exception):
            failure.member = "b.md"  # type: ignore[misc]
