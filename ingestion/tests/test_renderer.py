"""Unit tests for app.renderer -- the ingestion-side client for the optional
`render` compose-profile headless-render microservice (T7). No network, no
DB: httpx is monkeypatched throughout."""

from __future__ import annotations

import httpx
import pytest
from app import renderer


class _FakeResponse:
    def __init__(self, status_code: int, json_body=None, text: str = ""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body


def test_render_page_returns_html_on_200(monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        assert json == {"url": "https://docs-fixture.dev/page"}
        return _FakeResponse(200, json_body={"html": "<html>rendered</html>"})

    monkeypatch.setattr(httpx, "post", fake_post)
    html = renderer.render_page("https://docs-fixture.dev/page")
    assert html == "<html>rendered</html>"


def test_render_page_returns_none_on_connect_error(monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", fake_post)
    assert renderer.render_page("https://docs-fixture.dev/page") is None


def test_render_page_returns_none_on_timeout(monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(httpx, "post", fake_post)
    assert renderer.render_page("https://docs-fixture.dev/page") is None


def test_render_page_returns_none_on_never_raises_for_any_httpx_error(monkeypatch):
    """Soft-fail contract: ANY httpx.HTTPError subclass must return None, not
    propagate -- this is what keeps an unreachable renderer from ever
    hanging or crashing a sync."""
    def fake_post(url, json=None, headers=None, timeout=None):
        raise httpx.RemoteProtocolError("server disconnected")

    monkeypatch.setattr(httpx, "post", fake_post)
    assert renderer.render_page("https://docs-fixture.dev/page") is None


@pytest.mark.parametrize("status_code", [400, 401, 500, 502, 504])
def test_render_page_returns_none_on_non_200(monkeypatch, status_code):
    def fake_post(url, json=None, headers=None, timeout=None):
        return _FakeResponse(status_code, text="error body")

    monkeypatch.setattr(httpx, "post", fake_post)
    assert renderer.render_page("https://docs-fixture.dev/page") is None


def test_render_page_returns_none_on_malformed_json(monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        return _FakeResponse(200, json_body=None)

    monkeypatch.setattr(httpx, "post", fake_post)
    assert renderer.render_page("https://docs-fixture.dev/page") is None


def test_render_page_returns_none_on_empty_html(monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        return _FakeResponse(200, json_body={"html": ""})

    monkeypatch.setattr(httpx, "post", fake_post)
    assert renderer.render_page("https://docs-fixture.dev/page") is None


def test_render_page_passes_explicit_client_timeout(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["timeout"] = timeout
        return _FakeResponse(200, json_body={"html": "<html>ok</html>"})

    monkeypatch.setattr(httpx, "post", fake_post)
    renderer.render_page("https://docs-fixture.dev/page")
    assert captured["timeout"] == renderer.RENDERER_CLIENT_TIMEOUT_S
    assert captured["timeout"] > 0


def test_render_page_sends_token_header_when_configured(monkeypatch):
    monkeypatch.setattr(renderer, "RENDERER_TOKEN", "secret-token")
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["headers"] = headers
        return _FakeResponse(200, json_body={"html": "<html>ok</html>"})

    monkeypatch.setattr(httpx, "post", fake_post)
    renderer.render_page("https://docs-fixture.dev/page")
    assert captured["headers"]["X-Render-Token"] == "secret-token"


def test_render_page_omits_token_header_when_not_configured(monkeypatch):
    monkeypatch.setattr(renderer, "RENDERER_TOKEN", "")
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["headers"] = headers
        return _FakeResponse(200, json_body={"html": "<html>ok</html>"})

    monkeypatch.setattr(httpx, "post", fake_post)
    renderer.render_page("https://docs-fixture.dev/page")
    assert "X-Render-Token" not in captured["headers"]
