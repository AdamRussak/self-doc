"""Wires up the `pytest_terminal_summary` hook defined in
`test_retrieval_quality.py`.

pytest only auto-registers hook implementations from `conftest.py` files (or
proper plugins) — hooks defined directly in a test module are inert and never
called, regardless of invocation cwd. This file exists solely to delegate to
that hook so the recall / per-source-recall summary actually prints, both for
`make eval` (invoked as `cd tests/eval && pytest -m eval`) and `make test`
(invoked as `cd tests && pytest`, which recursively collects this package).
"""

from __future__ import annotations

from . import test_retrieval_quality as _trq


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    _trq.pytest_terminal_summary(terminalreporter, exitstatus, config)
