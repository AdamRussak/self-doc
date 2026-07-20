"""CRUD + scheduling repo for `doc_sources` (the crawl-config columns added
by `db/init/02_sources_config.sql`).

Contract (see IMPLEMENTATION_PLAN.md / T-A2 dispatch): `app.config.SourceConfig`
is the ONE validator for every write path (`create_source`, `update_source`,
`import_from_yaml`). This module never re-implements URL/name/prefix
validation — a `SourceConfig` instance is required to write, and pydantic has
already enforced its invariants (valid `base_url`, `name` pattern,
`max_pages > 0`, `rate_limit_rps > 0`, the BFS-seed-filter check, ...) by the
time it reaches here.

`SourceRecord` carries the DB-only fields that have no `SourceConfig`
equivalent: `id`, `status`, `enabled`, `schedule_cron`, `last_synced`,
`last_status`, `proposed_by`, `created_at`.

Verification note (read before trusting this module): the ingestion test
container's Postgres port is NOT published to the host, so pytest cannot open
a `psycopg` connection to it. Accordingly this module is split so the
unverifiable surface is as small as possible:

  - PURE, unit-tested with NO database:
      `_row_to_record`, `_record_to_row` (round-trip mapping),
      `_cfg_to_write_values`, `_cfg_matches_record`,
      `parse_cron`, `cron_matches`, `validate_cron`, `_select_due_records`
      (the skip-unparseable-and-keep-going row filter used by `due_sources`).
  - DB-DEPENDENT, therefore NEVER EXECUTED by this test suite (only ever
    exercised manually via `docker exec .../psql`, see the task's Result
    block for a transcript):
      `list_sources`, `get_source`, `create_source`, `update_source`,
      `delete_source`, `set_status`, `set_schedule`, `set_enabled`,
      `due_sources`, `import_from_yaml`.
    These are intentionally thin: each does exactly one
    execute-and-fetch/write, then hands off to the pure mapping functions
    above.

Cron subset supported by `parse_cron`/`cron_matches` (documented here because
there is no dependency — no `croniter`, no `APScheduler` — implementing it):
a cron expression MUST be exactly 5 whitespace-separated fields
`minute hour day month weekday` (standard cron field order and ranges:
minute 0-59, hour 0-23, day 1-31, month 1-12, weekday 0-6 with 0=Sunday, per
the widely-used POSIX cron convention). Each field must be one of:

  - `*`                  — matches every value in the field's range
  - `*/N`                — every Nth value starting at the range floor
  - a bare integer, e.g. `5`
  - a comma-separated list of bare integers, e.g. `0,15,30,45`

Anything else — ranges (`1-5`), step-on-range (`1-10/2`), named months/days
(`JAN`, `MON`), the `?`/`L`/`W`/`#` special characters, or a field count other
than 5 — is REJECTED by `validate_cron`/`parse_cron` with a `ValueError`
naming exactly what was rejected. This is a deliberate fail-closed choice AT
WRITE TIME: `set_schedule` (and every other path that persists
`schedule_cron`) calls `validate_cron` before the UPDATE, so an unsupported
expression is rejected loudly at save time and never reaches the database.

READ TIME IS FAIL-SOFT, DELIBERATELY DIFFERENT: this codebase's established
principle is that one bad row must not halt an entire pass (a bad page
doesn't kill a sync; a bad config doesn't kill the service) — scheduling
follows the same rule. Because `validate_cron` guards every write path,
a `schedule_cron` in the database that `parse_cron` cannot handle is a
data-integrity anomaly, not an expected outcome (hand-edited SQL, a partially
applied migration, a manually patched row). `due_sources` therefore SKIPS
such a row — logging a structlog ERROR event naming the source and the bad
expression — and keeps evaluating every other row, rather than raising and
aborting the whole scheduler pass. Loud in logs, not fatal in behavior.

`update_source` vs the lifecycle mutators (`set_status`, `set_enabled`,
`set_schedule`): `update_source` overwrites ONLY the `SourceConfig`-shaped
columns (base_url/sitemap/include_prefixes/exclude_prefixes/max_pages/
language/rate_limit_rps/llms_txt) — it is NOT a full-row replace and never touches
`status`, `enabled`, `schedule_cron`, or `proposed_by`. Those four are
lifecycle/scheduling fields with different authorization implications than a
config edit, and are changed only via the dedicated `set_status`/
`set_enabled`/`set_schedule` functions. A caller must not assume one call to
`update_source` fully replaces a row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psycopg

from .config import SourceConfig, load_sources
from .logging_config import get_logger

logger = get_logger(component="sources_repo")

# --- SourceRecord -----------------------------------------------------------

# Canonical column order used by every SELECT in this module. `_row_to_record`
# is a pure positional mapping over exactly this tuple — keep them in sync.
SOURCE_COLUMNS: tuple[str, ...] = (
    "id",
    "name",
    "base_url",
    "sitemap",
    "include_prefixes",
    "exclude_prefixes",
    "max_pages",
    "language",
    "rate_limit_rps",
    "llms_txt",
    "schedule_cron",
    "enabled",
    "status",
    "proposed_by",
    "created_at",
    "last_synced",
    "last_status",
)

_SELECT_COLUMNS_SQL = ", ".join(SOURCE_COLUMNS)

VALID_STATUSES = ("active", "pending", "rejected")


@dataclass(frozen=True)
class SourceRecord:
    """A full `doc_sources` row: `SourceConfig`'s fields plus the DB-only
    ones (id/status/enabled/schedule_cron/last_synced/last_status/
    proposed_by/created_at)."""

    id: int
    name: str
    base_url: str
    sitemap: str | None
    include_prefixes: list[str]
    exclude_prefixes: list[str]
    max_pages: int | None
    language: str
    rate_limit_rps: float
    llms_txt: str
    schedule_cron: str | None
    enabled: bool
    status: str
    proposed_by: str | None
    created_at: datetime
    last_synced: datetime | None
    last_status: str | None


@dataclass(frozen=True)
class ImportResult:
    """Outcome of a one-way `sources.yaml` -> `doc_sources` import, keyed by
    source name so a caller can log/inspect exactly what changed."""

    created: list[str]
    updated: list[str]
    skipped: list[str]


# --- Pure mapping helpers (no DB access; unit-tested directly) -------------


def _row_to_record(row: tuple) -> SourceRecord:
    """Positional row tuple (in `SOURCE_COLUMNS` order) -> `SourceRecord`.

    Pure: takes/returns only plain values. This is exactly where
    psycopg's TEXT[] <-> `list[str]` round-tripping and field-order bugs
    would hide, so it is unit-tested with a plain tuple and no DB in
    `test_sources_repo.py`.
    """
    (
        id_,
        name,
        base_url,
        sitemap,
        include_prefixes,
        exclude_prefixes,
        max_pages,
        language,
        rate_limit_rps,
        llms_txt,
        schedule_cron,
        enabled,
        status,
        proposed_by,
        created_at,
        last_synced,
        last_status,
    ) = row
    return SourceRecord(
        id=id_,
        name=name,
        base_url=base_url,
        sitemap=sitemap,
        include_prefixes=list(include_prefixes) if include_prefixes is not None else [],
        exclude_prefixes=list(exclude_prefixes) if exclude_prefixes is not None else [],
        max_pages=max_pages if (max_pages is not None and max_pages > 0) else 100,
        language=language or "english",
        rate_limit_rps=rate_limit_rps if (rate_limit_rps is not None and rate_limit_rps > 0) else 1.0,
        llms_txt=llms_txt if llms_txt else "auto",
        schedule_cron=schedule_cron,
        enabled=bool(enabled),
        status=status,
        proposed_by=proposed_by,
        created_at=created_at,
        last_synced=last_synced,
        last_status=last_status,
    )


def _cfg_to_write_values(cfg: SourceConfig) -> tuple:
    """`SourceConfig` -> the plain-value tuple shared by every write path:
    `(base_url, sitemap, include_prefixes, exclude_prefixes, max_pages,
    language, rate_limit_rps, llms_txt)`. Pure — no DB, no I/O.

    `name` is deliberately excluded: `create_source` writes it once at
    insert time (a source's `name` is its stable identity, see
    `doc_sources.name UNIQUE`); `update_source` identifies the row by
    `source_id` and never renames it.
    """
    return (
        str(cfg.base_url),
        str(cfg.sitemap) if cfg.sitemap is not None else None,
        list(cfg.include_prefixes),
        list(cfg.exclude_prefixes),
        cfg.max_pages,
        cfg.language,
        cfg.rate_limit_rps,
        str(cfg.llms_txt),
    )


def _cfg_matches_record(cfg: SourceConfig, record: SourceRecord) -> bool:
    """True iff `record`'s `SourceConfig`-shaped fields already equal `cfg` —
    i.e. re-importing `cfg` over `record` would be a no-op. Pure; used by
    `import_from_yaml` to decide `updated` vs `skipped`."""
    return (
        str(cfg.base_url) == record.base_url
        and (str(cfg.sitemap) if cfg.sitemap is not None else None) == record.sitemap
        and list(cfg.include_prefixes) == record.include_prefixes
        and list(cfg.exclude_prefixes) == record.exclude_prefixes
        and cfg.max_pages == record.max_pages
        and cfg.language == record.language
        and abs(cfg.rate_limit_rps - record.rate_limit_rps) < 1e-9
        and cfg.llms_txt == record.llms_txt
    )


# --- Cron subset: pure parsing/matching, no DB, no croniter/APScheduler ----

_CRON_FIELD_RANGES: tuple[tuple[int, int], ...] = (
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day of month
    (1, 12),  # month
    (0, 6),  # day of week, 0=Sunday .. 6=Saturday
)
_CRON_FIELD_NAMES = ("minute", "hour", "day", "month", "weekday")


def _parse_cron_field(token: str, field_name: str, lo: int, hi: int) -> set[int]:
    """Parse one cron field. Supports `*`, `*/N`, a bare int, or a
    comma-separated list of bare ints. Anything else raises `ValueError`
    naming the offending token and field. Pure."""
    if token == "*":
        return set(range(lo, hi + 1))

    if token.startswith("*/"):
        step_text = token[2:]
        if not step_text.isdigit() or int(step_text) <= 0:
            raise ValueError(
                f"unsupported cron {field_name} field {token!r}: '*/N' requires a "
                "positive integer step (supported subset: '*', '*/N', or "
                "comma-separated integers — no ranges like '1-5')"
            )
        step = int(step_text)
        return {v for v in range(lo, hi + 1) if (v - lo) % step == 0}

    values: set[int] = set()
    for part in token.split(","):
        if not part.lstrip("-").isdigit():
            raise ValueError(
                f"unsupported cron {field_name} field token {part!r} in {token!r}: "
                "only '*', '*/N', or comma-separated integers are supported — "
                "no ranges ('1-5'), named values ('MON'/'JAN'), or '?'/'L'/'W'/'#'"
            )
        v = int(part)
        if not (lo <= v <= hi):
            raise ValueError(
                f"cron {field_name} value {v} out of range [{lo}, {hi}] in token {token!r}"
            )
        values.add(v)
    return values


def parse_cron(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    """Parse a 5-field cron expression into `(minutes, hours, days, months,
    weekdays)` sets of allowed integers. Raises `ValueError` (naming exactly
    what's unsupported) for anything outside the documented subset — see the
    module docstring. Pure."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(
            f"unsupported cron expression {expr!r}: expected exactly 5 "
            f"space-separated fields (minute hour day month weekday), got {len(fields)}"
        )
    return tuple(  # type: ignore[return-value]
        _parse_cron_field(token, name, lo, hi)
        for token, name, (lo, hi) in zip(fields, _CRON_FIELD_NAMES, _CRON_FIELD_RANGES)
    )


def validate_cron(expr: str) -> None:
    """Raise `ValueError` if `expr` is outside the supported cron subset.
    Pure — call this at the write boundary for anything that persists a
    `schedule_cron` value, so an unsupported expression is rejected loudly
    at save time rather than silently never firing at read time."""
    parse_cron(expr)


def _select_due_records(
    records: list[SourceRecord], now: datetime, log=None
) -> list[SourceRecord]:
    """Pure: given `SourceRecord`s already filtered by the caller to
    `enabled AND status='active' AND schedule_cron IS NOT NULL`, return those
    whose `schedule_cron` matches `now`.

    A record whose `schedule_cron` is unparseable is SKIPPED, not raised —
    logging a `due_sources_skipped_unparseable_cron` structlog ERROR event
    naming the source (id + name) and the bad expression — and evaluation
    continues over the remaining records. See the module docstring's
    "READ TIME IS FAIL-SOFT" section for why this differs from
    `validate_cron`'s fail-closed behavior at write time: one bad row must
    never halt scheduling for every other source.

    `log` defaults to this module's structlog logger; tests inject a
    recording stub to assert the ERROR event fires without needing a DB or
    stdout capture.
    """
    if log is None:
        log = logger

    due: list[SourceRecord] = []
    for record in records:
        assert record.schedule_cron is not None  # guaranteed by caller's WHERE clause
        try:
            matches = cron_matches(record.schedule_cron, now)
        except ValueError as exc:
            log.error(
                "due_sources_skipped_unparseable_cron",
                source_id=record.id,
                source=record.name,
                schedule_cron=record.schedule_cron,
                error=str(exc),
            )
            continue
        if matches:
            due.append(record)
    return due


def cron_matches(expr: str, now: datetime) -> bool:
    """True iff `now` falls on a minute matched by cron expression `expr`.
    Pure — raises `ValueError` via `parse_cron` for an unsupported `expr`
    rather than silently returning False (which would be indistinguishable
    from "not due yet")."""
    minutes, hours, days, months, weekdays = parse_cron(expr)
    # Python's `datetime.weekday()` is Monday=0..Sunday=6; cron's weekday
    # field is Sunday=0..Saturday=6 — convert once, here, in the one place
    # that matters.
    cron_weekday = (now.weekday() + 1) % 7
    return (
        now.minute in minutes
        and now.hour in hours
        and now.day in days
        and now.month in months
        and cron_weekday in weekdays
    )


# --- DB-dependent layer (never executed by this test suite) ----------------


def _fetch_one(conn: psycopg.Connection, source_id: int) -> tuple | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_SELECT_COLUMNS_SQL} FROM doc_sources WHERE id = %s",
            (source_id,),
        )
        return cur.fetchone()


def _fetch_by_name(conn: psycopg.Connection, name: str) -> tuple | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_SELECT_COLUMNS_SQL} FROM doc_sources WHERE name = %s",
            (name,),
        )
        return cur.fetchone()


def list_sources(conn: psycopg.Connection, *, status: str | None = None) -> list[SourceRecord]:
    """All `doc_sources` rows, optionally filtered by `status`, ordered by
    name. DB-dependent."""
    with conn.cursor() as cur:
        if status is None:
            cur.execute(f"SELECT {_SELECT_COLUMNS_SQL} FROM doc_sources ORDER BY name")
        else:
            cur.execute(
                f"SELECT {_SELECT_COLUMNS_SQL} FROM doc_sources WHERE status = %s ORDER BY name",
                (status,),
            )
        rows = cur.fetchall()
    return [_row_to_record(row) for row in rows]


def get_source(conn: psycopg.Connection, source_id: int) -> SourceRecord | None:
    """A single `doc_sources` row by id, or `None` if it doesn't exist.
    DB-dependent."""
    row = _fetch_one(conn, source_id)
    return _row_to_record(row) if row is not None else None


def create_source(
    conn: psycopg.Connection,
    cfg: SourceConfig,
    *,
    status: str = "active",
    proposed_by: str | None = None,
) -> int:
    """Insert a new `doc_sources` row from a validated `SourceConfig`,
    returning its id. `cfg` is the ONLY validation on this write path — no
    field of `cfg` is re-validated here. `status` is checked against
    `VALID_STATUSES` (mirroring the DB CHECK constraint) so a bad value
    fails fast in Python rather than as an opaque `CheckViolation`.
    DB-dependent."""
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status {status!r}: must be one of {VALID_STATUSES}")

    base_url, sitemap, include_prefixes, exclude_prefixes, max_pages, language, rate_limit_rps, llms_txt = (
        _cfg_to_write_values(cfg)
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO doc_sources
                (name, base_url, sitemap, include_prefixes, exclude_prefixes,
                 max_pages, language, rate_limit_rps, llms_txt, status, proposed_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                cfg.name,
                base_url,
                sitemap,
                include_prefixes,
                exclude_prefixes,
                max_pages,
                language,
                rate_limit_rps,
                llms_txt,
                status,
                proposed_by,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        source_id = row[0]
    if not conn.autocommit:
        conn.commit()
    return source_id


def update_source(conn: psycopg.Connection, source_id: int, cfg: SourceConfig) -> None:
    """Overwrite the `SourceConfig`-shaped columns of an existing row.
    `cfg` is the ONLY validation on this write path. Does not rename the row
    (`name` is immutable via this function; `source_id` is the identity) and
    does not touch `status`/`enabled`/`schedule_cron`/`proposed_by` — use
    `set_status` for status changes. DB-dependent."""
    base_url, sitemap, include_prefixes, exclude_prefixes, max_pages, language, rate_limit_rps, llms_txt = (
        _cfg_to_write_values(cfg)
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE doc_sources
            SET base_url = %s, sitemap = %s, include_prefixes = %s,
                exclude_prefixes = %s, max_pages = %s, language = %s,
                rate_limit_rps = %s, llms_txt = %s
            WHERE id = %s
            """,
            (
                base_url,
                sitemap,
                include_prefixes,
                exclude_prefixes,
                max_pages,
                language,
                rate_limit_rps,
                llms_txt,
                source_id,
            ),
        )
    if not conn.autocommit:
        conn.commit()


def delete_source(conn: psycopg.Connection, source_id: int) -> None:
    """Delete a `doc_sources` row. `ON DELETE CASCADE` on
    `doc_pages.source_id` (and transitively `doc_chunks.page_id`, see
    `db/init/01_schema.sql`) means this also deletes every page and chunk
    that belonged to it. DB-dependent — cascade behavior verified manually
    against a throwaway database, not by this test suite (see Result)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM doc_sources WHERE id = %s", (source_id,))
    if not conn.autocommit:
        conn.commit()


def set_status(conn: psycopg.Connection, source_id: int, status: str) -> None:
    """Update just the `status` column. Validated against `VALID_STATUSES`
    (mirrors the DB CHECK constraint) before hitting the DB. DB-dependent."""
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status {status!r}: must be one of {VALID_STATUSES}")
    with conn.cursor() as cur:
        cur.execute("UPDATE doc_sources SET status = %s WHERE id = %s", (status, source_id))
    if not conn.autocommit:
        conn.commit()


def set_schedule(conn: psycopg.Connection, source_id: int, schedule_cron: str | None) -> None:
    """Update just the `schedule_cron` column. Validated via `validate_cron`
    BEFORE the UPDATE executes, so an unsupported cron expression is rejected
    (`ValueError`) without writing anything — fail-closed at write time (see
    module docstring). `schedule_cron=None` is explicitly accepted and means
    "no schedule / never auto-fire": `due_sources`' `schedule_cron IS NOT
    NULL` WHERE clause guarantees a source with `schedule_cron=None` is never
    returned as due, regardless of `enabled`/`status`. DB-dependent."""
    if schedule_cron is not None:
        validate_cron(schedule_cron)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE doc_sources SET schedule_cron = %s WHERE id = %s",
            (schedule_cron, source_id),
        )
    if not conn.autocommit:
        conn.commit()


def set_enabled(conn: psycopg.Connection, source_id: int, enabled: bool) -> None:
    """Update just the `enabled` column. `enabled=False` guarantees
    `due_sources`' `enabled = TRUE` WHERE clause excludes this source
    regardless of `status`/`schedule_cron`. DB-dependent."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE doc_sources SET enabled = %s WHERE id = %s",
            (enabled, source_id),
        )
    if not conn.autocommit:
        conn.commit()


def due_sources(conn: psycopg.Connection, now: datetime) -> list[SourceRecord]:
    """Sources that are `enabled`, `status='active'`, have a non-null
    `schedule_cron`, and whose cron expression matches `now`. A pending or
    disabled source (or one with no `schedule_cron` at all) is NEVER
    returned — enforced by the SQL WHERE clause.

    A row whose `schedule_cron` is unparseable is SKIPPED (logged loudly,
    not raised) rather than halting the whole pass — see `_select_due_records`
    and the module docstring's "READ TIME IS FAIL-SOFT" section. DB-dependent
    (the query); row selection/cron matching is delegated to the pure
    `_select_due_records`.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_SELECT_COLUMNS_SQL} FROM doc_sources
            WHERE enabled = TRUE AND status = 'active' AND schedule_cron IS NOT NULL
            ORDER BY name
            """
        )
        rows = cur.fetchall()

    records = [_row_to_record(row) for row in rows]
    return _select_due_records(records, now)


def import_from_yaml(conn: psycopg.Connection, path: Path) -> ImportResult:
    """One-way `sources.yaml` -> `doc_sources` import, upserting by `name`.

    - A name not yet in `doc_sources` is created (`status='active'`,
      `proposed_by=None`).
    - A name already present whose config-shaped columns differ from the
      yaml is updated in place.
    - A name already present whose config-shaped columns already match the
      yaml is left untouched (`skipped`) — this is what makes a second run
      idempotent.

    Never writes back to `path` — this is one-way (yaml -> db) only.
    `load_sources` (the same loader `sources.yaml` startup validation uses)
    is the only validation applied; DB-dependent.
    """
    configs = load_sources(path)

    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []

    for cfg in configs:
        row = _fetch_by_name(conn, cfg.name)
        if row is None:
            create_source(conn, cfg)
            created.append(cfg.name)
            continue

        existing = _row_to_record(row)
        if _cfg_matches_record(cfg, existing):
            skipped.append(cfg.name)
        else:
            update_source(conn, existing.id, cfg)
            updated.append(cfg.name)

    return ImportResult(created=created, updated=updated, skipped=skipped)
