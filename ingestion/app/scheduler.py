"""In-process cron scheduler for automated documentation re-crawling.

Why this exists: makes every scheduling decision an explicit structlog event with
a distinct name (`fired` / `skipped-not-due` / `skipped-locked` / `errored`)
so an operator can always answer "why didn't source X sync last night?" from
logs alone.

NO NEW DEPENDENCY: this reuses `app.sources_repo`'s existing cron subset
(`parse_cron` / `cron_matches` / `_select_due_records`) — no `croniter`, no
`APScheduler`.

Two public surfaces, per the T-B2 dispatch contract:

  - `next_due(records, now)` — PURE, table-tested, no DB. Given a batch of
    `SourceRecord`s (any mix of enabled/disabled, any status, any
    `schedule_cron`), returns exactly those that are enabled, `status="active"`,
    have a non-null `schedule_cron`, and whose cron expression matches `now`.
    Delegates the actual cron matching (and its skip-unparseable-and-log-ERROR
    fail-soft behavior) to `sources_repo._select_due_records` rather than
    reimplementing it — the enabled/status/schedule_cron-not-null filter here
    is the only logic this function owns.
  - `run_scheduler(stop)` — the loop. Wakes roughly every
    `poll_interval_seconds` (default 60s), fetches candidate sources, applies
    `next_due`, and triggers a sync for each due source that (a) hasn't
    already fired in the current minute-bucket (double-fire guard) and (b)
    isn't blocked by a sync already in flight (lock contention -> a logged
    no-op, never a concurrent crawl — two concurrent crawls against the same
    source would corrupt the page-purge accounting).

DB/sync/lock access is behind three module-level, monkeypatchable seams —
`fetch_candidates_fn`, `trigger_sync_fn`, `is_sync_locked_fn` — following the
same test-seam pattern `app.main` uses for `store.crawler.crawl`. Production
wiring (pointing these at a real DB connection, the real `/sync` code path,
and the shared `main._sync_lock`) happens in `main.py` startup (task B5, not
this module) — until wired, the default `trigger_sync_fn` raises loudly
rather than silently never syncing, and the default `is_sync_locked_fn`
reports "never locked" (a no-op scheduler must fail loud, not fail quiet).
Tests replace all three seams directly, so scheduling logic is exercised with
no live Postgres connection and no FastAPI app — pytest cannot reach Postgres
from the host, so anything requiring one would SKIP; nothing here does.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from .logging_config import get_logger
from .sources_repo import SourceRecord, _select_due_records

logger = get_logger(component="scheduler")

# How often the loop wakes to check for due sources. A module-level plain
# float (not a function) so tests can shrink it to make many ticks happen in
# well under a second of real wall-clock time.
poll_interval_seconds: float = 60.0


# --- Pure scheduling decision -----------------------------------------------


def next_due(records: list[SourceRecord], now: datetime) -> list[SourceRecord]:
    """Pure: which of `records` are due to sync at `now`.

    A record qualifies only if `enabled` AND `status == "active"` AND
    `schedule_cron is not None` AND its cron expression matches `now` — the
    same four conditions `sources_repo.due_sources`' SQL WHERE clause plus
    cron match enforce, just evaluated in Python over an arbitrary
    already-fetched list instead of via a live query. No DB, no I/O.

    Cron matching (including the fail-soft "skip an unparseable
    `schedule_cron`, log a structlog ERROR, keep going" behavior) is
    delegated entirely to `sources_repo._select_due_records` — this function
    never reimplements or duplicates that logic.
    """
    candidates = [
        r for r in records if r.enabled and r.status == "active" and r.schedule_cron is not None
    ]
    return _select_due_records(candidates, now, log=logger)


# --- Injectable seams (DB / sync / lock boundary) ---------------------------
#
# Production code never calls these default implementations directly through
# a hardcoded reference inside `run_scheduler` — it always goes through the
# module-level name below, so a test (or `main.py` startup wiring) can
# rebind `scheduler.fetch_candidates_fn` / `scheduler.trigger_sync_fn` /
# `scheduler.is_sync_locked_fn` / `scheduler.now_fn` wholesale.


def _default_now() -> datetime:
    return datetime.now(UTC)


def _default_fetch_candidates() -> list[SourceRecord]:
    """DB-dependent default: every `doc_sources` row, unfiltered — `next_due`
    owns all the enabled/status/schedule_cron/cron-match filtering, so this
    seam only needs to hand it a full, unfiltered batch. Never executed by
    this test suite (pytest cannot reach Postgres from the host); tests
    replace `fetch_candidates_fn` directly."""
    from . import (
        sources_repo,
        store,  # local import: keep a hard DB dependency out of the pure test path
    )

    conn = store.get_connection()
    try:
        return sources_repo.list_sources(conn)
    finally:
        conn.close()


async def _default_trigger_sync(names: list[str]) -> None:
    """Placeholder sync trigger. Production wiring (task B5, in `main.py`
    startup) replaces `trigger_sync_fn` with a callable that runs the same
    code path `POST /sync` uses. Raises if never wired, so a misconfigured
    deployment fails loudly instead of silently never syncing anything."""
    raise RuntimeError(
        "scheduler.trigger_sync_fn was never wired to a real sync path — "
        "see main.py startup wiring (task B5)"
    )


def _default_is_sync_locked() -> bool:
    """Placeholder lock-contention check. Production wiring (task B5)
    replaces `is_sync_locked_fn` with a check against the shared
    `main._sync_lock` — the SAME lock `POST /sync` uses — so a scheduled run
    starting while a manual run is in flight is a clean, logged no-op rather
    than a concurrent crawl. Defaults to "never locked" so an unwired
    scheduler doesn't silently refuse to fire; real double-crawl protection
    comes from the real lock once wired."""
    return False


now_fn: Callable[[], datetime] = _default_now
fetch_candidates_fn: Callable[[], list[SourceRecord]] = _default_fetch_candidates
trigger_sync_fn: Callable[[list[str]], Awaitable[None]] = _default_trigger_sync
is_sync_locked_fn: Callable[[], bool] = _default_is_sync_locked


def _not_due_reason(record: SourceRecord) -> str:
    """Best-effort human-readable reason a candidate wasn't in `next_due`'s
    result, for the `skipped-not-due` log line. Purely diagnostic — never
    changes scheduling behavior."""
    if not record.enabled:
        return "disabled"
    if record.status != "active":
        return f"status={record.status!r}"
    if record.schedule_cron is None:
        return "no-schedule"
    return "cron-not-due"


async def run_scheduler(stop: asyncio.Event) -> None:
    """The scheduler loop. Wakes roughly every `poll_interval_seconds`,
    fetches candidates via `fetch_candidates_fn`, filters to due sources via
    `next_due`, and fires `trigger_sync_fn` for each — subject to a
    per-minute-bucket double-fire guard and `is_sync_locked_fn` lock
    contention. Exits promptly once `stop` is set: sleeping is done via
    `asyncio.wait_for(stop.wait(), timeout=...)`, which returns immediately
    on `stop.set()` instead of blocking for the full poll interval — a
    scheduler that blocks shutdown turns every deploy into a SIGKILL wait.

    Every per-source decision this loop makes is logged as a structlog event
    with a distinct name — `fired`, `skipped-not-due`, `skipped-locked`,
    `errored` — so an operator can answer "why didn't source X sync last
    night?" from logs alone. An exception raised while fetching candidates,
    or while triggering one source's sync, is caught and logged; it never
    kills the loop or blocks later sources in the same pass from firing.

    `last_fired_minute` is local to a single `run_scheduler` call (not
    module state) — it maps `source.id -> minute_bucket` and is the explicit
    double-fire guard: a source due at 03:00 that's already fired for the
    03:00 bucket is skipped (logged `skipped-not-due`, reason
    `already-fired-this-window`) even if the loop wakes again inside the
    same minute.
    """
    log = logger.bind(component="scheduler")
    last_fired_minute: dict[int, datetime] = {}

    while not stop.is_set():
        now = now_fn()
        minute_bucket = now.replace(second=0, microsecond=0)

        try:
            candidates = await asyncio.to_thread(fetch_candidates_fn)
        except Exception as e:  # noqa: BLE001 - a bad fetch must not kill the loop
            log.error("scheduler_fetch_failed", error=str(e))
            candidates = []

        due = next_due(candidates, now)
        due_ids = {r.id for r in due}

        for record in candidates:
            if record.id not in due_ids:
                log.info(
                    "skipped-not-due",
                    source=record.name,
                    source_id=record.id,
                    reason=_not_due_reason(record),
                )

        for record in due:
            if last_fired_minute.get(record.id) == minute_bucket:
                log.info(
                    "skipped-not-due",
                    source=record.name,
                    source_id=record.id,
                    reason="already-fired-this-window",
                )
                continue

            if is_sync_locked_fn():
                log.info("skipped-locked", source=record.name, source_id=record.id)
                continue

            try:
                await trigger_sync_fn([record.name])
            except Exception as e:  # noqa: BLE001 - one source failing must not kill the loop
                log.error("errored", source=record.name, source_id=record.id, error=str(e))
            else:
                last_fired_minute[record.id] = minute_bucket
                log.info("fired", source=record.name, source_id=record.id)

        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_interval_seconds)
        except TimeoutError:
            pass
