"""Unit tests for the new /api/v1 REST endpoints in app.main."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ["SYNC_TOKEN"] = "test-token-123"


def _make_record(id: int, name: str, *, status: str = "active"):
    from app.sources_repo import SourceRecord

    return SourceRecord(
        id=id,
        name=name,
        base_url=f"https://{name}.example.com/",
        sitemap=None,
        include_prefixes=[],
        exclude_prefixes=[],
        max_pages=10,
        language="english",
        rate_limit_rps=1.0,
        schedule_cron=None,
        enabled=True,
        status=status,
        proposed_by=None,
        created_at=datetime.now(UTC),
        last_synced=None,
        last_status=None,
        llms_txt="auto",
    )


@pytest.fixture(scope="module")
def app_module():
    os.environ["SYNC_TOKEN"] = "test-token-123"
    os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
    os.environ.setdefault("POSTGRES_PORT", "5433")
    os.environ.setdefault("POSTGRES_USER", "self_docs")
    os.environ.setdefault("POSTGRES_PASSWORD", "testpass123")
    os.environ.setdefault("POSTGRES_DB", "self_docs")

    import app.sources_repo as sources_repo
    import app.store as store

    class _FakeConn:
        def close(self):
            pass

    canned = [_make_record(1, "fastapi"), _make_record(2, "nextjs")]
    with (
        patch.object(store, "get_connection", return_value=_FakeConn()),
        patch.object(sources_repo, "list_sources", return_value=canned),
    ):
        import app.main as main_mod

        yield main_mod


@pytest.fixture
def client(app_module):
    return TestClient(app_module.app)


AUTH_HEADERS = {"Authorization": "Bearer test-token-123"}


def test_api_search_unauthorized(client):
    response = client.get("/api/v1/search?q=test")
    assert response.status_code == 401


def test_api_search_empty_query(client):
    response = client.get("/api/v1/search?q=   ", headers=AUTH_HEADERS)
    assert response.status_code == 400
    assert "cannot be empty" in response.json()["detail"]


@patch("app.store.get_connection")
@patch("app.store.search_chunks")
def test_api_search_success(mock_search_chunks, mock_get_conn, client):
    mock_conn = MagicMock()
    mock_get_conn.return_value = mock_conn
    mock_search_chunks.return_value = [
        {
            "id": 1,
            "source": "fastapi",
            "heading_path": "Tutorial > Path",
            "url": "https://fastapi.tiangolo.com/",
            "score": 0.05,
            "snippet": "Path params snippet",
        }
    ]

    response = client.get("/api/v1/search?q=fastapi&limit=3", headers=AUTH_HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["id"] == 1
    assert data[0]["source"] == "fastapi"
    mock_search_chunks.assert_called_once_with(mock_conn, query="fastapi", source=None, limit=3)


@patch("app.store.get_connection")
@patch("app.store.get_chunk_by_id")
def test_api_get_chunk_found(mock_get_chunk_by_id, mock_get_conn, client):
    mock_conn = MagicMock()
    mock_get_conn.return_value = mock_conn
    mock_get_chunk_by_id.return_value = {
        "id": 42,
        "source": "fastapi",
        "heading_path": "Path Parameters",
        "url": "https://fastapi.tiangolo.com/",
        "content": "# Path Parameters",
        "fetched_at": "2026-07-21T00:00:00Z",
    }

    response = client.get("/api/v1/chunks/42", headers=AUTH_HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == 42
    assert data["content"] == "# Path Parameters"


@patch("app.store.get_connection")
@patch("app.store.get_chunk_by_id")
def test_api_get_chunk_not_found(mock_get_chunk_by_id, mock_get_conn, client):
    mock_conn = MagicMock()
    mock_get_conn.return_value = mock_conn
    mock_get_chunk_by_id.return_value = None

    response = client.get("/api/v1/chunks/999", headers=AUTH_HEADERS)
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


@patch("app.store.get_connection")
@patch("app.store.get_source_tree")
def test_api_get_tree(mock_get_source_tree, mock_get_conn, client):
    mock_conn = MagicMock()
    mock_get_conn.return_value = mock_conn
    mock_get_source_tree.return_value = [
        {
            "id": 1,
            "name": "fastapi",
            "base_url": "https://fastapi.tiangolo.com/",
            "page_count": 10,
            "chunk_count": 50,
            "last_synced": "2026-07-21T00:00:00Z",
        }
    ]

    response = client.get("/api/v1/tree", headers=AUTH_HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["name"] == "fastapi"
