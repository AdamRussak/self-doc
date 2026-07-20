"""Unit and integration tests for scripts/push_sources.py."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure repo root is on sys.path so we can import scripts.push_sources
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import push_sources


def test_compute_csrf_token():
    token = "test-secret-token"
    csrf1 = push_sources.compute_csrf_token(token)
    csrf2 = push_sources.compute_csrf_token(token)
    assert csrf1 == csrf2
    assert len(csrf1) == 64  # SHA-256 hex digest length
    assert push_sources.compute_csrf_token("different-token") != csrf1


def test_validate_source_item_valid():
    item = {
        "name": "fastapi",
        "base_url": "https://fastapi.tiangolo.com/",
        "max_pages": 500,
        "sitemap": "https://fastapi.tiangolo.com/sitemap.xml",
        "include_prefixes": ["/tutorial/"],
    }
    errors = push_sources.validate_source_item(item, 0)
    assert errors == []


def test_validate_source_item_invalid_name():
    item = {"name": "Invalid Name!", "base_url": "https://example.com/", "max_pages": 10}
    errors = push_sources.validate_source_item(item, 0)
    assert any("'name' must be a non-empty string matching ^[a-z0-9-]+$" in err for err in errors)


def test_validate_source_item_invalid_url():
    item = {"name": "fastapi", "base_url": "ftp://example.com/", "max_pages": 10}
    errors = push_sources.validate_source_item(item, 0)
    assert any("'base_url' must be a valid http(s) URL" in err for err in errors)


def test_validate_source_item_missing_max_pages():
    item = {"name": "fastapi", "base_url": "https://example.com/"}
    errors = push_sources.validate_source_item(item, 0)
    assert any("'max_pages' is required and must be a positive integer" in err for err in errors)

    item["max_pages"] = 0
    errors = push_sources.validate_source_item(item, 0)
    assert any("'max_pages' is required and must be a positive integer" in err for err in errors)


def test_validate_source_item_off_host_sitemap():
    item = {
        "name": "fastapi",
        "base_url": "https://example.com/docs",
        "max_pages": 100,
        "sitemap": "https://evil.com/sitemap.xml",
    }
    errors = push_sources.validate_source_item(item, 0)
    assert any("sitemap host 'evil.com' differs from base_url host 'example.com'" in err for err in errors)


def test_prepare_form_data():
    item = {
        "name": "fastapi",
        "base_url": "https://fastapi.tiangolo.com/",
        "max_pages": 250,
        "include_prefixes": ["/tutorial/", "/reference/"],
        "exclude_prefixes": ["/blog/"],
        "language": "English ",
        "rate_limit_rps": 2.5,
        "llms_txt": "only",
    }
    csrf_token = "mock-csrf-hex"
    form = push_sources.prepare_form_data(item, csrf_token)

    assert form["name"] == "fastapi"
    assert form["base_url"] == "https://fastapi.tiangolo.com/"
    assert form["max_pages"] == "250"
    assert form["include_prefixes"] == "/tutorial/\n/reference/"
    assert form["exclude_prefixes"] == "/blog/"
    assert form["language"] == "english"
    assert form["rate_limit_rps"] == "2.5"
    assert form["llms_txt"] == "only"
    assert form["csrf_token"] == "mock-csrf-hex"


def test_prepare_form_data_defaults():
    item = {
        "name": "minimal",
        "base_url": "http://minimal.dev/",
        "max_pages": 10,
    }
    form = push_sources.prepare_form_data(item, "csrf-token")
    assert form["include_prefixes"] == ""
    assert form["exclude_prefixes"] == ""
    assert form["language"] == "english"
    assert form["rate_limit_rps"] == "1.0"
    assert form["llms_txt"] == "auto"


def test_main_preflight_error(tmp_path, monkeypatch, capsys):
    json_file = tmp_path / "sources.json"
    json_file.write_text(json.dumps([{"name": "bad_name", "base_url": "https://example.com"}]), encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["push_sources.py", "--file", str(json_file), "--token", "secret"])
    exit_code = push_sources.main()
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Pre-flight validation failed" in err
    assert "'max_pages' is required" in err


def test_main_auth_failure(tmp_path, monkeypatch):
    json_file = tmp_path / "sources.json"
    json_file.write_text(json.dumps([{"name": "fastapi", "base_url": "https://example.com", "max_pages": 10}]), encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["push_sources.py", "--file", str(json_file), "--token", "secret"])

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_login = MagicMock()
    mock_login.status_code = 401
    mock_client.post.return_value = mock_login
    mock_client.cookies = {}

    with patch("httpx.Client", return_value=mock_client):
        exit_code = push_sources.main()
        assert exit_code == 1
        mock_client.post.assert_called_once_with("/admin/login", data={"token": "secret"})


def test_main_success_push_and_sync(tmp_path, monkeypatch, capsys):
    json_file = tmp_path / "sources.json"
    json_file.write_text(
        json.dumps([
            {"name": "src1", "base_url": "https://src1.com", "max_pages": 100},
            {"name": "src2", "base_url": "https://src2.com", "max_pages": 50},
        ]),
        encoding="utf-8",
    )

    monkeypatch.setattr(sys, "argv", ["push_sources.py", "--file", str(json_file), "--token", "mytoken", "--sync-after"])

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_login = MagicMock()
    mock_login.status_code = 303
    mock_client.cookies = {"admin_session": "cookie_val"}
    mock_client.headers = {}

    mock_post_new = MagicMock()
    mock_post_new.status_code = 303

    mock_sync = MagicMock()
    mock_sync.status_code = 202

    def side_effect(url, **kwargs):
        if url == "/admin/login":
            return mock_login
        if url == "/admin/sources/new":
            return mock_post_new
        if url == "/sync":
            return mock_sync
        raise ValueError(f"Unexpected url {url}")

    mock_client.post.side_effect = side_effect

    with patch("httpx.Client", return_value=mock_client):
        exit_code = push_sources.main()
        assert exit_code == 0
        assert mock_client.post.call_count == 5  # 1 login + 2 new + 2 sync
        captured = capsys.readouterr()
        assert "Created active : 2" in captured.out


def test_main_continue_on_error(tmp_path, monkeypatch, capsys):
    json_file = tmp_path / "sources.json"
    json_file.write_text(
        json.dumps([
            {"name": "src1", "base_url": "https://src1.com", "max_pages": 100},
            {"name": "src2", "base_url": "https://src2.com", "max_pages": 50},
        ]),
        encoding="utf-8",
    )

    monkeypatch.setattr(sys, "argv", ["push_sources.py", "--file", str(json_file), "--token", "mytoken", "--continue-on-error"])

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_login = MagicMock()
    mock_login.status_code = 303
    mock_client.cookies = {"admin_session": "cookie_val"}
    mock_client.headers = {}

    mock_post_fail = MagicMock()
    mock_post_fail.status_code = 400
    mock_post_fail.text = "source name 'src1' already exists"

    mock_post_ok = MagicMock()
    mock_post_ok.status_code = 303

    def side_effect(url, **kwargs):
        if url == "/admin/login":
            return mock_login
        if url == "/admin/sources/new":
            if kwargs["data"]["name"] == "src1":
                return mock_post_fail
            return mock_post_ok
        raise ValueError(f"Unexpected url {url}")

    mock_client.post.side_effect = side_effect

    with patch("httpx.Client", return_value=mock_client):
        exit_code = push_sources.main()
        assert exit_code == 0  # --continue-on-error turns partial failures into non-fatal
        captured = capsys.readouterr()
        assert "Source 'src1' already exists on server." in captured.err
        assert "Created active : 1" in captured.out
        assert "Skipped/Failed : 1" in captured.out
