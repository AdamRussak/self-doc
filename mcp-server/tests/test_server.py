"""Tests for app.server tool wrappers (no db/network — retrieval.search is
monkeypatched out)."""

from __future__ import annotations

import app.server as server


def test_search_docs_clamps_limit_above_max(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server.retrieval,
        "search",
        lambda query, source=None, limit=5: calls.append(limit) or "ok",
    )
    server.search_docs("query", limit=999)
    assert calls == [20]


def test_search_docs_clamps_limit_below_min(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server.retrieval,
        "search",
        lambda query, source=None, limit=5: calls.append(limit) or "ok",
    )
    server.search_docs("query", limit=-5)
    assert calls == [1]

    calls.clear()
    server.search_docs("query", limit=0)
    assert calls == [1]


def test_search_docs_passes_through_valid_limit(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server.retrieval,
        "search",
        lambda query, source=None, limit=5: calls.append(limit) or "ok",
    )
    server.search_docs("query", limit=7)
    assert calls == [7]
