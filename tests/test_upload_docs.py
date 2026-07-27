"""Unit tests for scripts/upload_docs.py."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure repo root is on sys.path so we can import scripts.upload_docs
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import upload_docs

# --- compute_csrf_token (copied verbatim from push_sources.py) -------------------------------


def test_compute_csrf_token_matches_push_sources():
    from scripts import push_sources

    token = "test-secret-token"
    assert upload_docs.compute_csrf_token(token) == push_sources.compute_csrf_token(token)
    assert len(upload_docs.compute_csrf_token(token)) == 64  # SHA-256 hex digest length
    assert upload_docs.compute_csrf_token("different-token") != upload_docs.compute_csrf_token(token)


# --- collect_files -----------------------------------------------------------------------------


def test_collect_files_directory_walk_by_suffix(tmp_path):
    (tmp_path / "keep.md").write_text("# a")
    (tmp_path / "keep.MARKDOWN").write_text("# a")
    (tmp_path / "keep.txt").write_text("a")
    (tmp_path / "keep.html").write_text("<p>a</p>")
    (tmp_path / "keep.HTM").write_text("<p>a</p>")
    (tmp_path / "keep.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "keep.zip").write_bytes(b"PK\x03\x04")
    (tmp_path / "skip.exe").write_bytes(b"nope")
    (tmp_path / "skip.png").write_bytes(b"nope")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "nested.md").write_text("# nested")

    entries, warnings = upload_docs.collect_files([tmp_path])

    assert warnings == []
    names = {name for _path, name in entries}
    assert names == {
        "keep.md",
        "keep.MARKDOWN",
        "keep.txt",
        "keep.html",
        "keep.HTM",
        "keep.pdf",
        "keep.zip",
        "sub/nested.md",
    }
    assert "skip.exe" not in names
    assert "skip.png" not in names


def test_collect_files_single_file_used_directly_regardless_of_extension(tmp_path):
    odd = tmp_path / "notes.rst"
    odd.write_text("hello")

    entries, warnings = upload_docs.collect_files([odd])

    assert warnings == []
    assert len(entries) == 1
    assert entries[0] == (odd, "notes.rst")


def test_collect_files_missing_path_warns_not_crashes(tmp_path):
    missing = tmp_path / "does-not-exist"

    entries, warnings = upload_docs.collect_files([missing])

    assert entries == []
    assert len(warnings) == 1
    assert "Path not found" in warnings[0]
    assert str(missing) in warnings[0]


# --- batch_files ---------------------------------------------------------------------------------


def _make_files(tmp_path, sizes: dict[str, int]) -> list[upload_docs.FileEntry]:
    entries = []
    for name, size in sizes.items():
        p = tmp_path / name
        p.write_bytes(b"x" * size)
        entries.append((p, name))
    return entries


def test_batch_files_respects_max_file_count(tmp_path):
    entries = _make_files(tmp_path, {f"f{i}.md": 10 for i in range(25)})

    batches = upload_docs.batch_files(entries, max_files=20, max_bytes=10_000_000)

    assert len(batches) == 2
    assert len(batches[0]) == 20
    assert len(batches[1]) == 5


def test_batch_files_respects_max_bytes(tmp_path):
    # 5 files x 3MB = 15MB; with a 10MB cap, should split into >1 batch.
    entries = _make_files(tmp_path, {f"f{i}.md": 3 * 1024 * 1024 for i in range(5)})

    batches = upload_docs.batch_files(entries, max_files=20, max_bytes=10 * 1024 * 1024)

    assert len(batches) == 2
    for batch in batches:
        total = sum(p.stat().st_size for p, _name in batch)
        assert total <= 10 * 1024 * 1024


def test_batch_files_oversized_single_file_ships_alone(tmp_path):
    huge = tmp_path / "huge.pdf"
    huge.write_bytes(b"x" * (12 * 1024 * 1024))  # exceeds the 10MB test cap alone
    small = tmp_path / "small.md"
    small.write_bytes(b"x" * 10)

    entries = [(small, "small.md"), (huge, "huge.pdf"), (small, "small.md")]
    batches = upload_docs.batch_files(entries, max_files=20, max_bytes=10 * 1024 * 1024)

    huge_batches = [b for b in batches if any(name == "huge.pdf" for _p, name in b)]
    assert len(huge_batches) == 1
    assert len(huge_batches[0]) == 1  # huge.pdf sent alone, not bundled with the small files


def test_batch_files_empty_input():
    assert upload_docs.batch_files([]) == []


# --- resolve_source_id ---------------------------------------------------------------------------


ADMIN_HTML_ACTIVE = """
<select name="source_id" required>
  <option value="" disabled selected>-- Select --</option>
  <option value="3">my-docs (upload://my-docs)</option>
  <option value="7">fastapi-docs (https://fastapi.tiangolo.com/)</option>
</select>
"""

ADMIN_HTML_PENDING_ONLY = """
<table class="table">
  <tr>
    <td><strong>proposed-source</strong></td>
    <td>https://example.com</td>
    <td class="actions">
      <a class="btn" href="/admin/sources/42">view/edit</a>
    </td>
  </tr>
</table>
"""


def test_resolve_source_id_from_active_select():
    assert upload_docs.resolve_source_id(ADMIN_HTML_ACTIVE, "my-docs") == 3
    assert upload_docs.resolve_source_id(ADMIN_HTML_ACTIVE, "fastapi-docs") == 7


def test_resolve_source_id_from_pending_fallback():
    assert upload_docs.resolve_source_id(ADMIN_HTML_PENDING_ONLY, "proposed-source") == 42


def test_resolve_source_id_not_found_returns_none():
    assert upload_docs.resolve_source_id(ADMIN_HTML_ACTIVE, "nonexistent") is None


# --- extract_error_message ------------------------------------------------------------------------


def test_extract_error_message_json_detail():
    resp = MagicMock()
    resp.headers = {"content-type": "application/json"}
    resp.json.return_value = {"detail": "request body too large"}
    resp.text = '{"detail": "request body too large"}'
    assert upload_docs.extract_error_message(resp) == "request body too large"


def test_extract_error_message_html_error_div():
    resp = MagicMock()
    resp.headers = {"content-type": "text/html; charset=utf-8"}
    resp.text = '<div class="card"><div class="error">source \'x\' is source_type=\'crawl\', not \'upload\'</div></div>'
    msg = upload_docs.extract_error_message(resp)
    assert "not 'upload'" in msg


def test_extract_error_message_fallback_truncates():
    resp = MagicMock()
    resp.headers = {"content-type": "text/plain"}
    resp.text = "x" * 500
    msg = upload_docs.extract_error_message(resp)
    assert len(msg) == 200


# --- main(): login/CSRF flow + batch upload + error handling --------------------------------------


def _mock_client(login_status=303, admin_status=200, admin_html=ADMIN_HTML_ACTIVE):
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.cookies = {"admin_session": "cookie_val"}
    mock_client.headers = {}

    mock_login = MagicMock()
    mock_login.status_code = login_status

    mock_admin = MagicMock()
    mock_admin.status_code = admin_status
    mock_admin.text = admin_html

    return mock_client, mock_login, mock_admin


def test_main_login_and_csrf_flow_matches_push_sources_pattern(tmp_path, monkeypatch):
    (tmp_path / "doc.md").write_text("# hi")
    monkeypatch.setattr(sys, "argv", ["upload_docs.py", "--source", "my-docs", "--token", "secret", str(tmp_path)])

    mock_client, mock_login, mock_admin = _mock_client()
    mock_upload = MagicMock()
    mock_upload.status_code = 303

    def side_effect(url, **kwargs):
        if url == "/admin/login":
            return mock_login
        if url == "/admin/sources/3/upload":
            assert kwargs["data"]["csrf_token"] == upload_docs.compute_csrf_token("secret")
            return mock_upload
        raise ValueError(f"Unexpected post url {url}")

    mock_client.post.side_effect = side_effect
    mock_client.get.return_value = mock_admin

    with patch("httpx.Client", return_value=mock_client):
        exit_code = upload_docs.main()

    assert exit_code == 0
    mock_client.post.assert_any_call("/admin/login", data={"token": "secret"})
    mock_client.get.assert_any_call("/admin")


def test_main_source_not_found(tmp_path, monkeypatch, capsys):
    (tmp_path / "doc.md").write_text("# hi")
    monkeypatch.setattr(sys, "argv", ["upload_docs.py", "--source", "nonexistent", "--token", "secret", str(tmp_path)])

    mock_client, mock_login, mock_admin = _mock_client()
    mock_client.post.return_value = mock_login
    mock_client.get.return_value = mock_admin

    with patch("httpx.Client", return_value=mock_client):
        exit_code = upload_docs.main()

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "No source named 'nonexistent' found" in err


def test_main_409_reported_cleanly_not_a_crash(tmp_path, monkeypatch, capsys):
    (tmp_path / "doc.md").write_text("# hi")
    monkeypatch.setattr(sys, "argv", ["upload_docs.py", "--source", "my-docs", "--token", "secret", str(tmp_path)])

    mock_client, mock_login, mock_admin = _mock_client()

    mock_conflict = MagicMock()
    mock_conflict.status_code = 409
    mock_conflict.headers = {"content-type": "text/html"}
    mock_conflict.text = '<div class="error">source \'my-docs\' is source_type=\'crawl\', not \'upload\'</div>'

    def side_effect(url, **kwargs):
        if url == "/admin/login":
            return mock_login
        if url == "/admin/sources/3/upload":
            return mock_conflict
        raise ValueError(f"Unexpected post url {url}")

    mock_client.post.side_effect = side_effect
    mock_client.get.return_value = mock_admin

    with patch("httpx.Client", return_value=mock_client):
        exit_code = upload_docs.main()

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "HTTP 409" in err
    assert "not 'upload'" in err
    # No raw traceback / unhandled exception should have propagated.
    assert "Traceback" not in err


def test_main_413_reported_cleanly_not_a_crash(tmp_path, monkeypatch, capsys):
    (tmp_path / "doc.md").write_text("# hi")
    monkeypatch.setattr(sys, "argv", ["upload_docs.py", "--source", "my-docs", "--token", "secret", str(tmp_path)])

    mock_client, mock_login, mock_admin = _mock_client()

    mock_too_large = MagicMock()
    mock_too_large.status_code = 413
    mock_too_large.headers = {"content-type": "application/json"}
    mock_too_large.json.return_value = {"detail": "request body too large"}
    mock_too_large.text = '{"detail": "request body too large"}'

    def side_effect(url, **kwargs):
        if url == "/admin/login":
            return mock_login
        if url == "/admin/sources/3/upload":
            return mock_too_large
        raise ValueError(f"Unexpected post url {url}")

    mock_client.post.side_effect = side_effect
    mock_client.get.return_value = mock_admin

    with patch("httpx.Client", return_value=mock_client):
        exit_code = upload_docs.main()

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "HTTP 413" in err
    assert "request body too large" in err
    assert "Traceback" not in err


def test_main_continue_on_error_exits_zero_despite_failures(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.md").write_text("# a")
    (tmp_path / "b.md").write_text("# b")
    monkeypatch.setattr(
        sys, "argv", ["upload_docs.py", "--source", "my-docs", "--token", "secret", "--continue-on-error", str(tmp_path)]
    )

    mock_client, mock_login, mock_admin = _mock_client()

    mock_fail = MagicMock()
    mock_fail.status_code = 400
    mock_fail.headers = {"content-type": "text/html"}
    mock_fail.text = '<div class="error">No parsable content found in the uploaded file(s).</div>'

    def side_effect(url, **kwargs):
        if url == "/admin/login":
            return mock_login
        if url == "/admin/sources/3/upload":
            return mock_fail
        raise ValueError(f"Unexpected post url {url}")

    mock_client.post.side_effect = side_effect
    mock_client.get.return_value = mock_admin

    with patch("httpx.Client", return_value=mock_client):
        exit_code = upload_docs.main()

    # Mirrors push_sources.py's --continue-on-error convention: exit 0 even
    # though failures occurred (the summary/stderr output signals them).
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Failed      : 2" in captured.out
    assert "No parsable content" in captured.err


def test_main_auth_failure(tmp_path, monkeypatch):
    (tmp_path / "doc.md").write_text("# hi")
    monkeypatch.setattr(sys, "argv", ["upload_docs.py", "--source", "my-docs", "--token", "secret", str(tmp_path)])

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_login = MagicMock()
    mock_login.status_code = 401
    mock_client.post.return_value = mock_login
    mock_client.cookies = {}

    with patch("httpx.Client", return_value=mock_client):
        exit_code = upload_docs.main()
        assert exit_code == 1
        mock_client.post.assert_called_once_with("/admin/login", data={"token": "secret"})


def test_main_no_token_fatal(tmp_path, monkeypatch, capsys):
    (tmp_path / "doc.md").write_text("# hi")
    monkeypatch.setattr(sys, "argv", ["upload_docs.py", "--source", "my-docs", str(tmp_path)])
    monkeypatch.delenv("SYNC_TOKEN", raising=False)

    exit_code = upload_docs.main()

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "SYNC_TOKEN must be provided" in err


def test_main_no_files_found_fatal(tmp_path, monkeypatch, capsys):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    monkeypatch.setattr(sys, "argv", ["upload_docs.py", "--source", "my-docs", "--token", "secret", str(empty_dir)])

    exit_code = upload_docs.main()

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "No uploadable files found" in err
