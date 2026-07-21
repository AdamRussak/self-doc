"""Tests for app.scheduler.

Everything below runs with NO database and NO real sleeps beyond small
event-loop scheduling overhead:

  - `next_due` is pure and table-tested directly (no async, no DB).
  - `run_scheduler` is driven with `asyncio.run(...)` (no pytest-asyncio
    dependency is added — none is declared in pyproject.toml) and every
    external dependency (`now_fn`, `fetch_candidates_fn`, `trigger_sync_fn`,
    `is_sync_locked_fn`, `poll_interval_seconds`) is monkeypatched to an
    injected fake, so the loop's *decision logic* is exercised without ever
    touching Postgres or a real clock. `poll_interval_seconds` is shrunk to a
    few milliseconds and each fake `fetch_candidates_fn`/`now_fn` sets the
    `stop` event once the test has observed what it needs — no test sleeps
    more than a fraction of a second, and the whole file's tests together
    total well under 1s of real sleeping.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from app import scheduler
from app.sources_repo import SourceRecord

NOW = datetime(2026, 7, 19, 3, 0, 0)


def _make_record(
    *,
    id_: int,
    name: str,
    enabled: bool = True,
    status: str = "active",
    schedule_cron: str | None = "0 3 * * *",
) -> SourceRecord:
    return SourceRecord(
        id=id_,
        name=name,
        base_url="https://example.com/docs/",
        sitemap=None,
        include_prefixes=[],
        exclude_prefixes=[],
        max_pages=10,
        language="english",
        rate_limit_rps=1.0,
        llms_txt="auto",
        schedule_cron=schedule_cron,
        enabled=enabled,
        status=status,
        proposed_by=None,
        created_at=datetime(2026, 1, 1),
        last_synced=None,
        last_status=None,
    )


# --- next_due: pure, table-driven (>= 8 cases), no DB -----------------------


@pytest.mark.parametrize(
    "record_kwargs, now, expect_due",
    [
        pytest.param(dict(schedule_cron="0 3 * * *"), NOW, True, id="basic-match"),
        pytest.param(dict(schedule_cron="0 9 * * *"), NOW, False, id="cron-does-not-match"),
        pytest.param(
            dict(schedule_cron="0 3 * * *", enabled=False), NOW, False, id="disabled-excluded"
        ),
        pytest.param(
            dict(schedule_cron="0 3 * * *", status="pending"),
            NOW,
            False,
            id="pending-status-excluded",
        ),
        pytest.param(dict(schedule_cron=None), NOW, False, id="no-schedule-never-due"),
        pytest.param(
            dict(schedule_cron="0 3 * * *"),
            datetime(2026, 7, 19, 3, 0, 59),
            True,
            id="minute-boundary-seconds-ignored",
        ),
        pytest.param(
            dict(schedule_cron="0 3 * * *"),
            datetime(2026, 7, 19, 3, 1, 0),
            False,
            id="minute-boundary-next-minute-not-due",
        ),
        pytest.param(
            dict(schedule_cron="1-5 * * * *"),
            NOW,
            False,
            id="unparseable-cron-skipped-not-raised",
        ),
    ],
)
def test_next_due_table(record_kwargs, now, expect_due) -> None:
    record = _make_record(id_=1, name="src", **record_kwargs)

    due = scheduler.next_due([record], now)

    assert bool(due) is expect_due
    if expect_due:
        assert due == [record]


def test_next_due_mixed_batch_returns_only_due_records_in_order() -> None:
    due_a = _make_record(id_=1, name="due-a", schedule_cron="0 3 * * *")
    not_due = _make_record(id_=2, name="not-due", schedule_cron="0 9 * * *")
    due_b = _make_record(id_=3, name="due-b", schedule_cron="0 3 * * *")
    disabled = _make_record(id_=4, name="disabled", schedule_cron="0 3 * * *", enabled=False)

    due = scheduler.next_due([due_a, not_due, due_b, disabled], NOW)

    assert [r.name for r in due] == ["due-a", "due-b"]


# --- run_scheduler: loop behavior, all DB/sync/lock seams faked -------------


def _run(coro, *, timeout: float = 2.0):
    """Run `coro` to completion with an overall safety-net timeout so a
    scheduler bug that fails to honor `stop` can't hang the test suite."""
    return asyncio.run(asyncio.wait_for(coro, timeout=timeout))


def test_no_double_fire_within_one_minute_injected_clock(monkeypatch) -> None:
    """Same source, same minute-bucket, multiple ticks -> exactly one fire.
    Uses an injected fixed clock and a tiny poll interval, never a real
    per-minute sleep."""
    record = _make_record(id_=1, name="src", schedule_cron="0 3 * * *")
    stop = asyncio.Event()
    ticks = {"n": 0}
    fire_calls: list[list[str]] = []

    def fake_fetch() -> list[SourceRecord]:
        ticks["n"] += 1
        if ticks["n"] >= 3:
            stop.set()
        return [record]

    async def fake_trigger(names: list[str]) -> None:
        fire_calls.append(names)

    monkeypatch.setattr(scheduler, "now_fn", lambda: NOW)
    monkeypatch.setattr(scheduler, "fetch_candidates_fn", fake_fetch)
    monkeypatch.setattr(scheduler, "trigger_sync_fn", fake_trigger)
    monkeypatch.setattr(scheduler, "is_sync_locked_fn", lambda: False)
    monkeypatch.setattr(scheduler, "poll_interval_seconds", 0.01)

    _run(scheduler.run_scheduler(stop))

    assert ticks["n"] >= 3, "expected multiple ticks to actually exercise the dedup guard"
    assert fire_calls == [["src"]], "must fire exactly once despite multiple ticks in the same minute"


def test_exception_in_one_source_does_not_kill_loop_and_later_source_still_fires(
    monkeypatch,
) -> None:
    """Two due sources in one tick; the first's trigger raises. The second
    must still fire, and the loop must not propagate the exception."""
    boom = _make_record(id_=1, name="boom", schedule_cron="0 3 * * *")
    ok = _make_record(id_=2, name="ok", schedule_cron="0 3 * * *")
    stop = asyncio.Event()
    attempted: list[str] = []

    def fake_fetch() -> list[SourceRecord]:
        stop.set()  # exactly one tick is enough to exercise this
        return [boom, ok]

    async def fake_trigger(names: list[str]) -> None:
        attempted.append(names[0])
        if names[0] == "boom":
            raise RuntimeError("simulated sync crash")

    monkeypatch.setattr(scheduler, "now_fn", lambda: NOW)
    monkeypatch.setattr(scheduler, "fetch_candidates_fn", fake_fetch)
    monkeypatch.setattr(scheduler, "trigger_sync_fn", fake_trigger)
    monkeypatch.setattr(scheduler, "is_sync_locked_fn", lambda: False)
    monkeypatch.setattr(scheduler, "poll_interval_seconds", 0.01)

    _run(scheduler.run_scheduler(stop))  # must not raise

    assert attempted == ["boom", "ok"], "both sources must be attempted in the same pass"


def test_fetch_failure_is_logged_and_does_not_kill_loop(monkeypatch) -> None:
    """An exception raised by `fetch_candidates_fn` itself (e.g. a dropped DB
    connection) must not kill the loop or prevent a later, successful tick
    from firing due sources."""
    record = _make_record(id_=1, name="src", schedule_cron="0 3 * * *")
    stop = asyncio.Event()
    ticks = {"n": 0}
    fire_calls: list[list[str]] = []

    def fake_fetch() -> list[SourceRecord]:
        ticks["n"] += 1
        if ticks["n"] == 1:
            raise RuntimeError("simulated DB connect failure")
        stop.set()
        return [record]

    async def fake_trigger(names: list[str]) -> None:
        fire_calls.append(names)

    monkeypatch.setattr(scheduler, "now_fn", lambda: NOW)
    monkeypatch.setattr(scheduler, "fetch_candidates_fn", fake_fetch)
    monkeypatch.setattr(scheduler, "trigger_sync_fn", fake_trigger)
    monkeypatch.setattr(scheduler, "is_sync_locked_fn", lambda: False)
    monkeypatch.setattr(scheduler, "poll_interval_seconds", 0.01)

    _run(scheduler.run_scheduler(stop))  # must not raise

    assert ticks["n"] == 2
    assert fire_calls == [["src"]], "the tick after the failed fetch must still fire normally"


def test_lock_contention_is_a_logged_no_op_not_an_exception(monkeypatch) -> None:
    """A sync already in flight (lock held) must skip firing cleanly — never
    raise, never trigger a second concurrent crawl."""
    record = _make_record(id_=1, name="src", schedule_cron="0 3 * * *")
    stop = asyncio.Event()
    fire_calls: list[list[str]] = []

    def fake_fetch() -> list[SourceRecord]:
        stop.set()
        return [record]

    async def fake_trigger(names: list[str]) -> None:
        fire_calls.append(names)

    monkeypatch.setattr(scheduler, "now_fn", lambda: NOW)
    monkeypatch.setattr(scheduler, "fetch_candidates_fn", fake_fetch)
    monkeypatch.setattr(scheduler, "trigger_sync_fn", fake_trigger)
    monkeypatch.setattr(scheduler, "is_sync_locked_fn", lambda: True)
    monkeypatch.setattr(scheduler, "poll_interval_seconds", 0.01)

    _run(scheduler.run_scheduler(stop))  # must not raise

    assert fire_calls == [], "a locked sync must never be triggered concurrently"


def test_shutdown_is_prompt_when_stop_is_set_during_a_long_poll_interval(monkeypatch) -> None:
    """The loop must not block shutdown for the full poll interval: setting
    `stop` mid-sleep should make `run_scheduler` return almost immediately,
    even with a long `poll_interval_seconds` — a scheduler that blocks
    shutdown turns every deploy into a SIGKILL wait."""
    stop = asyncio.Event()

    def fake_fetch() -> list[SourceRecord]:
        return []  # nothing due; the loop should just go straight to sleeping

    async def fake_trigger(names: list[str]) -> None:  # pragma: no cover - never called
        raise AssertionError("no source is due; trigger_sync_fn must not be called")

    monkeypatch.setattr(scheduler, "now_fn", lambda: NOW)
    monkeypatch.setattr(scheduler, "fetch_candidates_fn", fake_fetch)
    monkeypatch.setattr(scheduler, "trigger_sync_fn", fake_trigger)
    monkeypatch.setattr(scheduler, "is_sync_locked_fn", lambda: False)
    monkeypatch.setattr(scheduler, "poll_interval_seconds", 60.0)  # deliberately long

    async def _drive() -> float:
        task = asyncio.create_task(scheduler.run_scheduler(stop))
        await asyncio.sleep(0.05)  # let the loop reach its sleep-until-stop wait
        loop = asyncio.get_event_loop()
        start = loop.time()
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)
        return loop.time() - start

    elapsed = _run(_drive())

    assert elapsed < 1.0, "run_scheduler must exit promptly after stop is set, not after 60s"
