"""Prometheus metric objects + the single recording helper for sync outcomes.

WHY A SEPARATE MODULE (not `store.py`, not `main.py`):

  - `store.py` is the data layer and, prior to this module, had no
    dependency on `prometheus_client` at all. Adding the metric *objects*
    directly to `store.py` would be a reasonable alternative, but this repo
    keeps "what /metrics exposes" as a standalone concern importable by any
    layer (route handlers, the admin blueprint, the scheduler) without
    forcing every one of those to reach into the data layer's module
    namespace for unrelated exposition objects.
  - `main.py` is where these lived before this task, but incrementing them
    only from `main.py`'s own request-handler code is exactly the bug this
    task fixes (`admin.py` never touches `main.py`, and must not import it —
    see `admin.py`'s own module docstring on the circular-import
    constraint), so the metric objects can no longer live solely in
    `main.py` if `admin.py`'s sync paths are also to update them.
  - A tiny leaf module with zero imports of `store` or `main` sidesteps the
    circular-import risk entirely: `store.py` imports `metrics` (a data
    layer depending on a leaf exposition module is unremarkable), and
    `main.py` re-exports the same objects (`from .metrics import
    PAGES_FETCHED, ...`) so existing call sites (`main.PAGES_FETCHED` in
    tests, the `/metrics` route) keep working unchanged -- same objects,
    same names, same registry, just defined once here instead of twice.

RECORDING, NOT JUST DECLARING: `record_sync_outcome` is the one place that
knows how a `SourceOutcome` + a wall-clock duration map onto the seven
samples below, so every caller (currently: `store.sync_source_with_metrics`,
which every admin.py and main.py sync entrypoint routes through) gets
identical semantics -- including the `SYNC_LAST_SUCCESS` gauge, which is set
for BOTH "ok" and "partial" (a partial sync did index pages; only "failed"
must leave staleness alerts firing) -- without duplicating that if/else at
every call site.
"""

from __future__ import annotations

import time
from typing import Protocol

from prometheus_client import Counter, Gauge, Histogram

PAGES_FETCHED = Counter(
    "pages_fetched_total", "Pages fetched and (re)indexed (new or changed)", ["source"]
)
PAGES_SKIPPED = Counter(
    "pages_skipped_unchanged_total", "Pages skipped because their content hash is unchanged", ["source"]
)
PAGES_NOT_MODIFIED = Counter(
    "pages_not_modified_total", "Pages skipped via HTTP 304 conditional request", ["source"]
)
PAGES_SOFT_FAILED = Counter(
    "pages_soft_failed_total", "Pages soft-failed due to expected site quirks (404/503 fetch or stub content)", ["source"]
)
CHUNKS_INDEXED = Counter("chunks_indexed_total", "Chunks written to doc_chunks", ["source"])
SYNC_DURATION = Histogram("sync_duration_seconds", "Duration of a full sync run for one source", ["source"])
SYNC_LAST_SUCCESS = Gauge(
    "sync_last_success_timestamp", "Unix timestamp of the last successful (status=ok) sync", ["source"]
)


class _SyncOutcomeLike(Protocol):
    """Structural type for the `SourceOutcome`-shaped object this module
    reads. Declared here (rather than importing `app.store.SourceOutcome`)
    so `metrics.py` stays a zero-internal-import leaf module."""

    pages_fetched: int
    pages_skipped: int
    pages_not_modified: int
    pages_soft_failed: int
    chunks_indexed: int
    status: str


def record_sync_outcome(source_name: str, outcome: _SyncOutcomeLike, duration_seconds: float) -> None:
    """Move all seven `/metrics` samples for one source's completed sync.

    `status="ok"` and `status="partial"` both set `SYNC_LAST_SUCCESS` to
    "now" (a partial sync did index pages -- only `status="failed"` must
    leave the gauge untouched, since that's the one alert rules read as
    "sync attempted, but nothing usable came of it").
    """
    PAGES_FETCHED.labels(source=source_name).inc(outcome.pages_fetched)
    PAGES_SKIPPED.labels(source=source_name).inc(outcome.pages_skipped)
    PAGES_NOT_MODIFIED.labels(source=source_name).inc(outcome.pages_not_modified)
    PAGES_SOFT_FAILED.labels(source=source_name).inc(outcome.pages_soft_failed)
    CHUNKS_INDEXED.labels(source=source_name).inc(outcome.chunks_indexed)
    SYNC_DURATION.labels(source=source_name).observe(duration_seconds)
    if outcome.status in ("ok", "partial"):
        SYNC_LAST_SUCCESS.labels(source=source_name).set(time.time())
