"""Tests for the boot-time shared-secret policy (app/security.py) and its
wiring into `app.main`'s import-time fail-fast block.

Two layers, on purpose:

  - Unit tests over the pure decision function, covering both required
    branches (loopback+placeholder -> warn/boot, non-loopback+placeholder ->
    refuse) plus the parsing edges that decide which branch you land in.
  - Subprocess boot tests, which are the only way to observe an *import-time*
    `SystemExit` in `app.main` (the same pattern
    `test_main.py::test_missing_sync_token_refuses_to_start` already uses).
    These run before any database connection is attempted, so they need no
    Postgres.

NO TEST IN THIS FILE CONTAINS A REAL SECRET. The "good" tokens are obviously
synthetic fixtures.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from app import security


def _ingestion_root() -> Path:
    return Path(__file__).parent.parent


# --- placeholder detection ---------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "change-me",  # the value .env.example used to ship
        "change_me",
        "CHANGE-ME",
        "  change-me  ",
        '"change-me"',
        "changeme",
        "REPLACE_ME_WITH_A_GENERATED_TOKEN",  # what .env.example ships now
        "replace-me",
        "your-token-here",
        "<your-token>",
        "secret",
        "password",
        "token",
        "changeit",
        "change-it",
        "",
        None,
    ],
)
def test_known_placeholders_are_detected(token):
    assert security.is_placeholder_token(token) is True


@pytest.mark.parametrize(
    "token",
    [
        "test-token-123",  # this suite's own fixture token
        "test-admin-token-xyz",  # test_admin.py's fixture token
        # Deliberately not hex/base64-shaped: a realistic-looking random
        # literal here would trip secret scanners on this repo forever, for a
        # value that is by construction not a secret.
        "a" * 64,
        "not-a-placeholder-just-a-fixture",
        "fixture-token-for-tests-only-do-not-use",
    ],
)
def test_non_placeholders_are_not_flagged(token):
    """A false positive here is an outage: it would refuse to boot a
    deployment holding a perfectly good secret."""
    assert security.is_placeholder_token(token) is False


# --- listener classification -------------------------------------------------------------


@pytest.mark.parametrize(
    "listener",
    ["127.0.0.1", "127.0.0.1:8080", "127.5.6.7:8080", "localhost", "localhost:8080", "[::1]:8080", "::1", "http://127.0.0.1:8080/"],
)
def test_loopback_listeners(listener):
    assert security.is_loopback_listener(listener) is True


@pytest.mark.parametrize(
    "listener",
    [
        "0.0.0.0:8080",
        "192.168.1.10:8080",
        "10.0.0.5",
        "[2001:db8::1]:8080",
        "docs-mcp.example.lan",
        "https://docs-mcp.example.lan/api/v1",
        "not a listener",  # unparseable -> treated as exposed (fail-closed)
        "",
    ],
)
def test_non_loopback_listeners(listener):
    assert security.is_loopback_listener(listener) is False


def test_parse_listeners_defaults_to_loopback_when_unset():
    assert security.parse_listeners(None) == ["127.0.0.1"]
    assert security.parse_listeners("") == ["127.0.0.1"]
    assert security.parse_listeners("  ,  ") == ["127.0.0.1"]


def test_parse_listeners_splits_and_strips():
    assert security.parse_listeners("127.0.0.1:8080, https://docs.example.lan/api/v1") == [
        "127.0.0.1:8080",
        "https://docs.example.lan/api/v1",
    ]


# --- the two required policy branches ----------------------------------------------------


def test_placeholder_token_on_loopback_only_warns_and_boots():
    """Branch 1 (acceptance criterion): loopback + placeholder -> warn, still boot."""
    result = security.evaluate_token_policy("change-me", ["127.0.0.1:8080"], var_name="SYNC_TOKEN")
    assert result.verdict == "warn"
    assert result.should_refuse is False
    assert "WARNING" in result.message
    assert "SYNC_TOKEN" in result.message
    # The remediation must be actionable from the message alone.
    assert "openssl rand -hex 32" in result.message
    assert "docs/runbook.md" in result.message


def test_placeholder_token_with_non_loopback_listener_refuses_to_boot():
    """Branch 2 (acceptance criterion): non-loopback + placeholder -> refuse,
    with a message naming both the offending listener and the fix."""
    result = security.evaluate_token_policy(
        "change-me",
        ["127.0.0.1:8080", "https://docs-mcp.example.lan/api/v1"],
        var_name="SYNC_TOKEN",
    )
    assert result.verdict == "refuse"
    assert result.should_refuse is True
    assert "FATAL" in result.message
    assert "SYNC_TOKEN" in result.message
    assert "docs-mcp.example.lan" in result.message
    assert "Refusing to start" in result.message
    assert "openssl rand -hex 32" in result.message


def test_refusal_message_never_contains_the_token_value():
    """A boot failure is logged, shipped to a log aggregator, and pasted into
    issues — the value must never travel with it."""
    for listeners in (["127.0.0.1:8080"], ["docs.example.lan"]):
        result = security.evaluate_token_policy("change-me", listeners)
        assert "change-me" not in result.message


def test_real_token_is_ok_on_any_listener():
    for listeners in (["127.0.0.1:8080"], ["0.0.0.0:8080"], ["https://docs.example.lan"]):
        result = security.evaluate_token_policy("test-token-123", listeners)
        assert result.verdict == "ok"
        assert result.should_refuse is False


def test_check_shared_token_reads_the_listeners_env_var():
    warn = security.check_shared_token("change-me", {"SELF_DOCS_LISTENERS": "127.0.0.1:8080"})
    assert warn.verdict == "warn"

    refuse = security.check_shared_token("change-me", {"SELF_DOCS_LISTENERS": "127.0.0.1:8080,docs.example.lan"})
    assert refuse.verdict == "refuse"

    # Unset -> permissive loopback default, so existing .env files keep working.
    assert security.check_shared_token("change-me", {}).verdict == "warn"


# --- end-to-end boot behavior (import-time SystemExit) -----------------------------------


def _boot_env(**overrides: str) -> dict[str, str]:
    """Env for a subprocess `import app.main`. POSTGRES_PORT=1 guarantees an
    immediate ECONNREFUSED for the cases that are expected to get *past* the
    token check, so the test never hangs waiting on a database."""
    env = os.environ.copy()
    env["POSTGRES_HOST"] = "127.0.0.1"
    env["POSTGRES_PORT"] = "1"
    env["POSTGRES_USER"] = "self_docs"
    env["POSTGRES_PASSWORD"] = "testpass123"
    env["POSTGRES_DB"] = "self_docs"
    env.pop("SELF_DOCS_LISTENERS", None)
    env.update(overrides)
    return env


def _import_main(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=str(_ingestion_root()),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_boot_refuses_when_placeholder_token_meets_non_loopback_listener():
    proc = _import_main(
        _boot_env(SYNC_TOKEN="change-me", SELF_DOCS_LISTENERS="127.0.0.1:8080,https://docs-mcp.example.lan/api/v1")
    )
    assert proc.returncode != 0
    assert "FATAL" in proc.stderr
    assert "SYNC_TOKEN" in proc.stderr
    assert "docs-mcp.example.lan" in proc.stderr
    assert "openssl rand -hex 32" in proc.stderr
    # It must die on the TOKEN, not limp on to the database check.
    assert "could not load sources" not in proc.stderr


def test_boot_only_warns_when_placeholder_token_is_loopback_only():
    """Must NOT refuse: it gets past the token gate and dies later on the
    unreachable database instead, which proves the token check let it through."""
    proc = _import_main(_boot_env(SYNC_TOKEN="change-me", SELF_DOCS_LISTENERS="127.0.0.1:8080"))
    assert "WARNING" in proc.stderr
    assert "SYNC_TOKEN" in proc.stderr
    assert "Refusing to start" not in proc.stderr
    # Proof it proceeded past the token gate: the next fail-fast check fired.
    assert "could not load sources" in proc.stderr


def test_boot_is_silent_about_the_token_when_it_is_not_a_placeholder():
    proc = _import_main(_boot_env(SYNC_TOKEN="test-token-123", SELF_DOCS_LISTENERS="https://docs-mcp.example.lan/api/v1"))
    assert "placeholder" not in proc.stderr
    assert "could not load sources" in proc.stderr
