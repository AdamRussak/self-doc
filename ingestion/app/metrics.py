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
knows how a `SourceOutcome` + a wall-clock duration map onto the samples
below, so every caller (currently: `store.sync_source_with_metrics`,
which every admin.py and main.py sync entrypoint routes through) gets
identical semantics -- including the `SYNC_LAST_SUCCESS` gauge, which is set
for BOTH "ok" and "partial" (a partial sync did index pages; only "failed"
must leave staleness alerts firing) -- without duplicating that if/else at
every call site.

STATUS AS A DIRECT SIGNAL (`SYNC_LAST_STATUS`): T4's `SourceSyncDegraded`
rule originally had to proxy "this source reported `partial`" via a
run-over-run page-volume collapse, because no metric exposed
`classify_sync`'s verdict directly. `SYNC_LAST_STATUS` closes that gap.

Shape chosen: a status-LABELLED gauge (`sync_last_status{source, status}`),
set to 1 for the status of the most recently completed sync -- the same
"enum as a set of 0/1 series" pattern used by e.g. kube-state-metrics'
`kube_pod_status_phase{phase="Running"} 1`. This was preferred over:
  - a per-status Counter (`sync_partial_total`, ...): would answer "did this
    happen at least once in the window" via `increase(...) > 0`, but not
    "what is the CURRENT status" as a single point-in-time fact -- a source
    that went partial once, then ok forever after, would keep matching
    `increase(sync_partial_total[30h]) > 0` for as long as that old partial
    sample is still inside the lookback window.
  - three separate per-status gauges (`sync_status_ok`, `sync_status_partial`,
    `sync_status_failed`): equivalent in spirit but a worse API -- three
    metric *names* to keep in sync instead of one metric with a `status`
    label, and no natural place to enumerate "what are the valid values".
A labelled gauge lets a rule assert the exact condition needed --
`sync_last_status{status="partial"} == 1` -- as an instant, current-state
check with no proxy and no time window at all.

THE STALE-SERIES PITFALL (why `record_sync_outcome` calls `.remove(...)`,
not just `.labels(...).set(...)`): a Gauge child series, once created by
`.labels(...)`, persists at its last-set value until explicitly removed --
`prometheus_client` does not auto-expire label combinations. If a source's
first sync is "partial" (creating
`sync_last_status{source="x", status="partial"} 1`) and its next sync is
"ok" (creating `sync_last_status{source="x", status="ok"} 1`), BOTH series
still exist and both still read `1` unless the "partial" series is
explicitly cleared -- so `sync_last_status{status="partial"} == 1` would
keep matching "x" forever, i.e. exactly the "looks permanently degraded"
bug this metric exists to avoid. `record_sync_outcome` therefore treats the
three statuses as mutually exclusive per source: it sets the *current*
status's series to 1 and removes (not merely zeroes) the other two, so
each source has exactly one active `sync_last_status` series at a time.
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
PAGES_FAILED = Counter(
    "pages_failed_total", "Pages that hard-failed during extract/chunk/embed/store (drives status=partial/failed)", ["source"]
)
PAGES_SHELL_SUSPECTED = Counter(
    "pages_shell_suspected_total",
    "Pages flagged by T6's cross-page JS-shell detector (short/duplicate-length extraction "
    "siblings suspected of being a client-side-rendered stub) — a subset of pages_soft_failed_total; "
    "some of these may go on to be recovered via the headless renderer (T7), see app/store.py",
    ["source"],
)
CHUNKS_INDEXED = Counter("chunks_indexed_total", "Chunks written to doc_chunks", ["source"])
SYNC_DURATION = Histogram("sync_duration_seconds", "Duration of a full sync run for one source", ["source"])
SYNC_LAST_SUCCESS = Gauge(
    "sync_last_success_timestamp", "Unix timestamp of the last successful (status=ok) sync", ["source"]
)
SYNC_LAST_STATUS = Gauge(
    "sync_last_status",
    "1 for the status of the most recently completed sync; the other two status series for "
    "the same source are removed so exactly one is active at a time (see module docstring)",
    ["source", "status"],
)

# The only three values `SourceOutcome.status` / `classify_sync` ever produce.
_SYNC_STATUSES = ("ok", "partial", "failed")


class _SyncOutcomeLike(Protocol):
    """Structural type for the `SourceOutcome`-shaped object this module
    reads. Declared here (rather than importing `app.store.SourceOutcome`)
    so `metrics.py` stays a zero-internal-import leaf module."""

    pages_fetched: int
    pages_skipped: int
    pages_not_modified: int
    pages_soft_failed: int
    pages_failed: int
    chunks_indexed: int
    shell_suspected_count: int
    status: str


def record_sync_outcome(source_name: str, outcome: _SyncOutcomeLike, duration_seconds: float) -> None:
    """Move all `/metrics` samples for one source's completed sync.

    `status="ok"` and `status="partial"` both set `SYNC_LAST_SUCCESS` to
    "now" (a partial sync did index pages -- only `status="failed"` must
    leave the gauge untouched, since that's the one alert rules read as
    "sync attempted, but nothing usable came of it").

    `SYNC_LAST_STATUS` is set to a single active series per source: 1 for
    `outcome.status`, with the other two status label combinations removed
    (see module docstring's "stale-series pitfall" section).
    """
    PAGES_FETCHED.labels(source=source_name).inc(outcome.pages_fetched)
    PAGES_SKIPPED.labels(source=source_name).inc(outcome.pages_skipped)
    PAGES_NOT_MODIFIED.labels(source=source_name).inc(outcome.pages_not_modified)
    PAGES_SOFT_FAILED.labels(source=source_name).inc(outcome.pages_soft_failed)
    PAGES_FAILED.labels(source=source_name).inc(outcome.pages_failed)
    PAGES_SHELL_SUSPECTED.labels(source=source_name).inc(outcome.shell_suspected_count)
    CHUNKS_INDEXED.labels(source=source_name).inc(outcome.chunks_indexed)
    SYNC_DURATION.labels(source=source_name).observe(duration_seconds)
    if outcome.status in ("ok", "partial"):
        SYNC_LAST_SUCCESS.labels(source=source_name).set(time.time())

    for status in _SYNC_STATUSES:
        if status == outcome.status:
            SYNC_LAST_STATUS.labels(source=source_name, status=status).set(1)
        else:
            SYNC_LAST_STATUS.remove(source_name, status)
