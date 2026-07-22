"""Tests for app.sources_repo.

Split to match the module's pure/DB-dependent split:

  - The pure mapping (`_row_to_record`, `_cfg_to_write_values`,
    `_cfg_matches_record`) and cron (`parse_cron`, `cron_matches`,
    `validate_cron`) tests below run everywhere, no DB required.
  - The DB-dependent tests (list/get/create/update/delete/set_status/
    due_sources/import_from_yaml exercised against a real connection) need a
    live Postgres reachable at POSTGRES_* env vars (the compose `db` service
    test overlay on 127.0.0.1:5433) and are skipped automatically otherwise —
    mirroring test_store.py / test_migration.py. They build the T-A2 target
    schema (01_schema.sql + 02_sources_config.sql) on a throwaway database
    and never touch the shared `self_docs` database.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import psycopg
import pytest
from app import sources_repo
from app.config import SourceConfig, load_sources
from app.embedder import EMBEDDING_DIM as _EMBEDDING_DIM
from app.sources_repo import (
    SOURCE_COLUMNS,
    ImportResult,
    SourceRecord,
    _cfg_matches_record,
    _cfg_to_write_values,
    _row_to_record,
    _select_due_records,
    cron_matches,
    parse_cron,
    validate_cron,
)

# --- Pure: SourceConfig -> row -> SourceRecord round-trip (no DB) ----------


def _make_cfg(**overrides) -> SourceConfig:
    defaults = dict(
        name="widget",
        base_url="https://widget.example.com/docs/",
        sitemap="https://widget.example.com/sitemap.xml",
        include_prefixes=["/docs/", "/api/"],
        exclude_prefixes=["/docs/changelog/"],
        max_pages=42,
        language="english",
        rate_limit_rps=2.5,
    )
    defaults.update(overrides)
    return SourceConfig.model_validate(defaults)


def _row_from_cfg(
    cfg: SourceConfig,
    *,
    id_: int = 7,
    status: str = "active",
    proposed_by: str | None = None,
    enabled: bool = True,
    schedule_cron: str | None = None,
    llms_txt: str | None = None,
    created_at: datetime = datetime(2026, 1, 1, 0, 0, 0),
    last_synced: datetime | None = None,
    last_status: str | None = None,
) -> tuple:
    """Build a plain row tuple in SOURCE_COLUMNS order the way a real
    `SELECT ... FROM doc_sources` would return it (psycopg hands back TEXT[]
    columns as plain Python lists already)."""
    values = {
        "id": id_,
        "name": cfg.name,
        "base_url": str(cfg.base_url),
        "sitemap": str(cfg.sitemap) if cfg.sitemap is not None else None,
        "include_prefixes": list(cfg.include_prefixes),
        "exclude_prefixes": list(cfg.exclude_prefixes),
        "max_pages": cfg.max_pages,
        "language": cfg.language,
        "rate_limit_rps": cfg.rate_limit_rps,
        "llms_txt": llms_txt if llms_txt is not None else str(cfg.llms_txt),
        "schedule_cron": schedule_cron,
        "enabled": enabled,
        "status": status,
        "proposed_by": proposed_by,
        "created_at": created_at,
        "last_synced": last_synced,
        "last_status": last_status,
    }
    return tuple(values[col] for col in SOURCE_COLUMNS)


def test_round_trip_cfg_to_row_to_record_field_for_field() -> None:
    cfg = _make_cfg()
    row = _row_from_cfg(cfg, id_=99, status="pending", proposed_by="alice")
    record = _row_to_record(row)

    assert record.id == 99
    assert record.name == "widget"
    assert record.base_url == "https://widget.example.com/docs/"
    assert record.sitemap == "https://widget.example.com/sitemap.xml"
    # The load-bearing bit: TEXT[] round-trips as a plain list, in order,
    # not a tuple/str/None.
    assert record.include_prefixes == ["/docs/", "/api/"]
    assert record.exclude_prefixes == ["/docs/changelog/"]
    assert isinstance(record.include_prefixes, list)
    assert isinstance(record.exclude_prefixes, list)
    assert record.max_pages == 42
    assert record.language == "english"
    assert record.rate_limit_rps == pytest.approx(2.5)
    assert record.status == "pending"
    assert record.proposed_by == "alice"
    assert record.enabled is True


def test_round_trip_null_array_columns_become_empty_lists() -> None:
    """A NULL include/exclude_prefixes column (shouldn't happen given the
    NOT NULL DEFAULT '{}', but defense in depth) maps to [] not None."""
    cfg = _make_cfg(include_prefixes=[], exclude_prefixes=[])
    row = list(_row_from_cfg(cfg))
    idx_include = SOURCE_COLUMNS.index("include_prefixes")
    idx_exclude = SOURCE_COLUMNS.index("exclude_prefixes")
    row[idx_include] = None
    row[idx_exclude] = None
    record = _row_to_record(tuple(row))
    assert record.include_prefixes == []
    assert record.exclude_prefixes == []


def test_cfg_to_write_values_matches_row_mapping() -> None:
    """_cfg_to_write_values (used by create_source/update_source) and
    _row_to_record (used by every read path) must agree on how a
    SourceConfig's fields map to plain values — this is the fidelity the
    round-trip test above exercises end to end."""
    cfg = _make_cfg()
    base_url, sitemap, include_prefixes, exclude_prefixes, max_pages, language, rate_limit_rps, llms_txt = (
        _cfg_to_write_values(cfg)
    )
    row = _row_from_cfg(cfg, id_=1)
    record = _row_to_record(row)
    assert record.base_url == base_url
    assert record.sitemap == sitemap
    assert record.include_prefixes == include_prefixes
    assert record.exclude_prefixes == exclude_prefixes
    assert record.max_pages == max_pages
    assert record.language == language
    assert record.rate_limit_rps == pytest.approx(rate_limit_rps)
    assert record.llms_txt == llms_txt


def test_cfg_to_write_values_sitemap_none_stays_none() -> None:
    cfg = _make_cfg(sitemap=None)
    _, sitemap, *_ = _cfg_to_write_values(cfg)
    assert sitemap is None


# --- Pure: _cfg_matches_record (import idempotency decision) ---------------


def test_cfg_matches_record_true_for_identical_config() -> None:
    cfg = _make_cfg()
    record = _row_to_record(_row_from_cfg(cfg))
    assert _cfg_matches_record(cfg, record) is True


def test_cfg_matches_record_false_when_max_pages_differs() -> None:
    cfg = _make_cfg()
    record = _row_to_record(_row_from_cfg(cfg))
    changed_cfg = _make_cfg(max_pages=999)
    assert _cfg_matches_record(changed_cfg, record) is False


def test_cfg_matches_record_false_when_prefixes_differ() -> None:
    cfg = _make_cfg()
    record = _row_to_record(_row_from_cfg(cfg))
    changed_cfg = _make_cfg(include_prefixes=["/docs/", "/api/", "/guides/"])
    assert _cfg_matches_record(changed_cfg, record) is False


def test_cfg_matches_record_false_when_only_llms_txt_differs() -> None:
    cfg = _make_cfg()
    record = _row_to_record(_row_from_cfg(cfg))
    changed_cfg = _make_cfg(llms_txt="off")
    assert _cfg_matches_record(changed_cfg, record) is False


def test_cfg_matches_record_true_when_llms_txt_also_matches() -> None:
    cfg = _make_cfg(llms_txt="only")
    record = _row_to_record(_row_from_cfg(cfg))
    assert _cfg_matches_record(cfg, record) is True


# --- SourceConfig rejects invalid input on both create and update paths ----


def test_invalid_config_rejected_missing_max_pages() -> None:
    with pytest.raises(Exception):
        SourceConfig.model_validate(
            {"name": "bad", "base_url": "https://example.com/"}
        )  # max_pages is required (gt=0, no default)


def test_invalid_config_rejected_bad_name_pattern() -> None:
    with pytest.raises(Exception):
        SourceConfig.model_validate(
            {"name": "Bad Name!", "base_url": "https://example.com/", "max_pages": 10}
        )


def test_invalid_config_rejected_unknown_key() -> None:
    with pytest.raises(Exception):
        SourceConfig.model_validate(
            {
                "name": "bad",
                "base_url": "https://example.com/",
                "max_pages": 10,
                "totally_unknown_field": True,
            }
        )


def test_invalid_config_rejected_negative_max_pages() -> None:
    """create_source/update_source both require a SourceConfig instance —
    proving pydantic rejects a bad value here proves both write paths reject
    it too, since neither implements a second validation pass."""
    with pytest.raises(Exception):
        SourceConfig.model_validate(
            {"name": "bad", "base_url": "https://example.com/", "max_pages": -5}
        )


# --- Pure: cron parsing / matching (table-tested, >=6 cases) ---------------


@pytest.mark.parametrize(
    ("expr", "now", "expected"),
    [
        # every minute
        ("* * * * *", datetime(2026, 7, 19, 3, 17), True),
        # exact minute/hour match
        ("30 4 * * *", datetime(2026, 7, 19, 4, 30), True),
        # exact minute/hour MISMATCH -> must NOT fire
        ("30 4 * * *", datetime(2026, 7, 19, 4, 31), False),
        # step: every 15 minutes, hour open
        ("*/15 * * * *", datetime(2026, 7, 19, 9, 45), True),
        ("*/15 * * * *", datetime(2026, 7, 19, 9, 44), False),
        # comma list of hours
        ("0 6,18 * * *", datetime(2026, 7, 19, 18, 0), True),
        ("0 6,18 * * *", datetime(2026, 7, 19, 12, 0), False),
        # weekday restriction: 2026-07-19 is a Sunday (cron weekday 0)
        ("0 0 * * 0", datetime(2026, 7, 19, 0, 0), True),
        ("0 0 * * 1", datetime(2026, 7, 19, 0, 0), False),
        # month restriction
        ("0 0 1 1 *", datetime(2026, 1, 1, 0, 0), True),
        ("0 0 1 1 *", datetime(2026, 7, 1, 0, 0), False),
    ],
)
def test_cron_matches_table(expr: str, now: datetime, expected: bool) -> None:
    assert cron_matches(expr, now) is expected


@pytest.mark.parametrize(
    "expr",
    [
        "1-5 * * * *",  # ranges unsupported
        "0 0 * JAN *",  # named month unsupported
        "0 0 * * MON",  # named weekday unsupported
        "* * * *",  # wrong field count
        "*/0 * * * *",  # non-positive step
        "0 99 * * *",  # out-of-range hour
    ],
)
def test_cron_rejects_unsupported_expressions(expr: str) -> None:
    with pytest.raises(ValueError):
        validate_cron(expr)
    with pytest.raises(ValueError):
        parse_cron(expr)
    with pytest.raises(ValueError):
        cron_matches(expr, datetime(2026, 7, 19, 0, 0))


def test_cron_parse_returns_expected_sets() -> None:
    minutes, hours, days, months, weekdays = parse_cron("0,30 * 1 * *")
    assert minutes == {0, 30}
    assert hours == set(range(0, 24))
    assert days == {1}
    assert months == set(range(1, 13))
    assert weekdays == set(range(0, 7))


# --- Pure: _select_due_records (fail-soft skip of an unparseable row) ------


class _RecordingLog:
    """Minimal structlog-shaped stub that records `.error(event, **fields)`
    calls so tests can assert a bad row is logged loudly without depending on
    stdout capture — mirrors `_RecordingLog` in test_crawler.py."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def error(self, event, **kwargs):
        self.events.append((event, kwargs))

    def bind(self, **kwargs):
        return self


def _make_record(
    *,
    id_: int,
    name: str,
    schedule_cron: str | None,
) -> SourceRecord:
    cfg = _make_cfg(name=name)
    row = _row_from_cfg(cfg, id_=id_, schedule_cron=schedule_cron)
    return _row_to_record(row)


def test_select_due_records_skips_unparseable_row_and_still_returns_others() -> None:
    """A bad row must not halt the pass: it is skipped, and rows both before
    and after it in iteration order are still evaluated correctly."""
    now = datetime(2026, 7, 19, 3, 0)
    good_before = _make_record(id_=1, name="good-before", schedule_cron="0 3 * * *")
    bad_row = _make_record(id_=2, name="bad-row", schedule_cron="1-5 * * * *")  # ranges unsupported
    good_after = _make_record(id_=3, name="good-after", schedule_cron="0 3 * * *")
    not_due = _make_record(id_=4, name="not-due", schedule_cron="0 9 * * *")

    log = _RecordingLog()
    due = _select_due_records([good_before, bad_row, good_after, not_due], now, log=log)

    names = {r.name for r in due}
    assert names == {"good-before", "good-after"}
    assert "bad-row" not in names
    assert "not-due" not in names


def test_select_due_records_logs_error_event_naming_source_and_expression() -> None:
    now = datetime(2026, 7, 19, 3, 0)
    bad_row = _make_record(id_=42, name="bad-cron-source", schedule_cron="not a cron")

    log = _RecordingLog()
    _select_due_records([bad_row], now, log=log)

    assert len(log.events) == 1
    event, fields = log.events[0]
    assert event == "due_sources_skipped_unparseable_cron"
    assert fields["source_id"] == 42
    assert fields["source"] == "bad-cron-source"
    assert fields["schedule_cron"] == "not a cron"
    assert "error" in fields


def test_select_due_records_uses_module_logger_by_default() -> None:
    """No `log=` kwarg must not raise -- confirms the default (module-level
    structlog logger) path works, not just the injected-stub path."""
    now = datetime(2026, 7, 19, 3, 0)
    bad_row = _make_record(id_=1, name="bad", schedule_cron="1-5 * * * *")
    due = _select_due_records([bad_row], now)
    assert due == []


# --- DB-dependent tests: skipped automatically without a live Postgres -----

os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("POSTGRES_PORT", "5433")
os.environ.setdefault("POSTGRES_USER", "self_docs")
os.environ.setdefault("POSTGRES_PASSWORD", "testpass123")

_THROWAWAY_DB = "sources_repo_test_pytest"


def _admin_connect():
    return psycopg.connect(
        host=os.environ["POSTGRES_HOST"],
        port=os.environ["POSTGRES_PORT"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname="postgres",
        autocommit=True,
    )


def _db_available() -> bool:
    try:
        conn = _admin_connect()
        conn.close()
        return True
    except psycopg.OperationalError:
        return False


pytestmark_live = pytest.mark.skipif(
    not _db_available(), reason="no live Postgres reachable for sources_repo integration tests"
)


@pytest.fixture()
def db_conn():
    """A connection to a fresh throwaway database with 01_schema.sql +
    02_sources_config.sql applied — never touches `self_docs`."""
    schema_sql = (
        Path(__file__).resolve().parents[2] / "db" / "init" / "01_schema.sql"
    ).read_text()
    migration_sql = (
        Path(__file__).resolve().parents[2] / "db" / "init" / "02_sources_config.sql"
    ).read_text()

    admin = _admin_connect()
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {_THROWAWAY_DB}")
        cur.execute(f"CREATE DATABASE {_THROWAWAY_DB}")
    admin.close()

    conn = psycopg.connect(
        host=os.environ["POSTGRES_HOST"],
        port=os.environ["POSTGRES_PORT"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=_THROWAWAY_DB,
        autocommit=True,
    )
    with conn.cursor() as cur:
        cur.execute(schema_sql)
        cur.execute(migration_sql)

    try:
        yield conn
    finally:
        conn.close()
        admin = _admin_connect()
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {_THROWAWAY_DB}")
        admin.close()


@pytestmark_live
def test_create_get_update_delete_round_trip(db_conn: psycopg.Connection) -> None:
    cfg = _make_cfg(name="db-widget")
    source_id = sources_repo.create_source(db_conn, cfg, status="pending", proposed_by="bot")

    record = sources_repo.get_source(db_conn, source_id)
    assert record is not None
    assert record.name == "db-widget"
    assert record.status == "pending"
    assert record.proposed_by == "bot"
    assert record.include_prefixes == ["/docs/", "/api/"]

    updated_cfg = _make_cfg(name="db-widget", max_pages=7)
    sources_repo.update_source(db_conn, source_id, updated_cfg)
    record = sources_repo.get_source(db_conn, source_id)
    assert record.max_pages == 7
    assert record.name == "db-widget"  # unchanged by update_source

    sources_repo.set_status(db_conn, source_id, "active")
    record = sources_repo.get_source(db_conn, source_id)
    assert record.status == "active"

    sources_repo.delete_source(db_conn, source_id)
    assert sources_repo.get_source(db_conn, source_id) is None


@pytestmark_live
def test_set_status_rejects_invalid_value(db_conn: psycopg.Connection) -> None:
    cfg = _make_cfg(name="db-widget-2")
    source_id = sources_repo.create_source(db_conn, cfg)
    with pytest.raises(ValueError):
        sources_repo.set_status(db_conn, source_id, "bogus")


@pytestmark_live
def test_list_sources_filters_by_status(db_conn: psycopg.Connection) -> None:
    sources_repo.create_source(db_conn, _make_cfg(name="active-one"), status="active")
    sources_repo.create_source(db_conn, _make_cfg(name="pending-one"), status="pending")

    active = sources_repo.list_sources(db_conn, status="active")
    pending = sources_repo.list_sources(db_conn, status="pending")
    everything = sources_repo.list_sources(db_conn)

    assert {r.name for r in active} == {"active-one"}
    assert {r.name for r in pending} == {"pending-one"}
    assert {r.name for r in everything} >= {"active-one", "pending-one"}


@pytestmark_live
def test_due_sources_excludes_pending_and_disabled(db_conn: psycopg.Connection) -> None:
    now = datetime(2026, 7, 19, 3, 0)

    due_id = sources_repo.create_source(db_conn, _make_cfg(name="due-active"), status="active")
    pending_id = sources_repo.create_source(db_conn, _make_cfg(name="due-pending"), status="pending")
    disabled_id = sources_repo.create_source(db_conn, _make_cfg(name="due-disabled"), status="active")
    not_due_id = sources_repo.create_source(db_conn, _make_cfg(name="not-due"), status="active")

    with db_conn.cursor() as cur:
        cur.execute("UPDATE doc_sources SET schedule_cron = %s WHERE id = %s", ("0 3 * * *", due_id))
        cur.execute("UPDATE doc_sources SET schedule_cron = %s WHERE id = %s", ("0 3 * * *", pending_id))
        cur.execute(
            "UPDATE doc_sources SET schedule_cron = %s, enabled = FALSE WHERE id = %s",
            ("0 3 * * *", disabled_id),
        )
        cur.execute("UPDATE doc_sources SET schedule_cron = %s WHERE id = %s", ("0 9 * * *", not_due_id))

    due = sources_repo.due_sources(db_conn, now)
    names = {r.name for r in due}
    assert names == {"due-active"}
    assert "due-pending" not in names
    assert "due-disabled" not in names
    assert "not-due" not in names


@pytestmark_live
def test_set_schedule_validates_before_writing(db_conn: psycopg.Connection) -> None:
    source_id = sources_repo.create_source(db_conn, _make_cfg(name="schedule-target"))

    with pytest.raises(ValueError):
        sources_repo.set_schedule(db_conn, source_id, "1-5 * * * *")  # unsupported: ranges

    record = sources_repo.get_source(db_conn, source_id)
    assert record.schedule_cron is None  # rejected before the UPDATE ran

    sources_repo.set_schedule(db_conn, source_id, "0 3 * * *")
    record = sources_repo.get_source(db_conn, source_id)
    assert record.schedule_cron == "0 3 * * *"


@pytestmark_live
def test_set_schedule_none_means_never_due(db_conn: psycopg.Connection) -> None:
    now = datetime(2026, 7, 19, 3, 0)
    source_id = sources_repo.create_source(db_conn, _make_cfg(name="schedule-none"), status="active")
    sources_repo.set_schedule(db_conn, source_id, "0 3 * * *")
    assert {r.id for r in sources_repo.due_sources(db_conn, now)} >= {source_id}

    sources_repo.set_schedule(db_conn, source_id, None)
    record = sources_repo.get_source(db_conn, source_id)
    assert record.schedule_cron is None
    assert source_id not in {r.id for r in sources_repo.due_sources(db_conn, now)}


@pytestmark_live
def test_set_enabled_false_excludes_from_due_sources(db_conn: psycopg.Connection) -> None:
    now = datetime(2026, 7, 19, 3, 0)
    source_id = sources_repo.create_source(db_conn, _make_cfg(name="enabled-target"), status="active")
    sources_repo.set_schedule(db_conn, source_id, "0 3 * * *")
    assert source_id in {r.id for r in sources_repo.due_sources(db_conn, now)}

    sources_repo.set_enabled(db_conn, source_id, False)
    record = sources_repo.get_source(db_conn, source_id)
    assert record.enabled is False
    assert source_id not in {r.id for r in sources_repo.due_sources(db_conn, now)}

    sources_repo.set_enabled(db_conn, source_id, True)
    record = sources_repo.get_source(db_conn, source_id)
    assert record.enabled is True
    assert source_id in {r.id for r in sources_repo.due_sources(db_conn, now)}


@pytestmark_live
def test_due_sources_skips_unparseable_row_against_real_db(db_conn: psycopg.Connection) -> None:
    """DB-dependent companion to the pure `_select_due_records` tests above:
    a hand-edited (bypassing `set_schedule`'s validation) unparseable
    `schedule_cron` row must not prevent other due sources from being
    returned."""
    now = datetime(2026, 7, 19, 3, 0)
    good_id = sources_repo.create_source(db_conn, _make_cfg(name="good-db-source"), status="active")
    bad_id = sources_repo.create_source(db_conn, _make_cfg(name="bad-db-source"), status="active")

    with db_conn.cursor() as cur:
        cur.execute("UPDATE doc_sources SET schedule_cron = %s WHERE id = %s", ("0 3 * * *", good_id))
        # Simulate a data-integrity anomaly: bypass set_schedule's validation
        # via a raw UPDATE, the way a hand-edited row or bad migration would.
        cur.execute("UPDATE doc_sources SET schedule_cron = %s WHERE id = %s", ("1-5 * * * *", bad_id))

    due = sources_repo.due_sources(db_conn, now)
    names = {r.name for r in due}
    assert names == {"good-db-source"}


@pytestmark_live
def test_import_from_yaml_imports_sources_and_is_idempotent(
    db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    yaml_file = tmp_path / "sources.yaml"
    yaml_file.write_text(
        """
sources:
  - name: test-s1
    base_url: https://s1.example.com/docs
    max_pages: 10
  - name: test-s2
    base_url: https://s2.example.com/docs
    max_pages: 20
"""
    )
    expected_names = {cfg.name for cfg in load_sources(yaml_file)}
    assert expected_names == {"test-s1", "test-s2"}

    result = sources_repo.import_from_yaml(db_conn, yaml_file)
    assert isinstance(result, ImportResult)
    assert set(result.created) == expected_names
    assert result.updated == []
    assert result.skipped == []

    rows = sources_repo.list_sources(db_conn)
    assert expected_names.issubset({r.name for r in rows})

    # Second run: everything already matches -> pure no-op, proving
    # idempotency.
    result2 = sources_repo.import_from_yaml(db_conn, yaml_file)
    assert result2.created == []
    assert result2.updated == []
    assert set(result2.skipped) == expected_names


@pytestmark_live
def test_delete_source_cascades_to_pages_and_chunks(db_conn: psycopg.Connection) -> None:
    source_id = sources_repo.create_source(db_conn, _make_cfg(name="cascade-me"))

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO doc_pages (source_id, url, content_hash) VALUES (%s, %s, %s) RETURNING id",
            (source_id, "https://widget.example.com/docs/a", "0" * 64),
        )
        (page_id,) = cur.fetchone()
        cur.execute(
            """
            INSERT INTO doc_chunks (page_id, heading_path, chunk_index, content, embedding)
            VALUES (%s, %s, %s, %s, %s::vector)
            """,
            (page_id, "Intro", 0, "hello world", "[" + ",".join(["0.0"] * _EMBEDDING_DIM) + "]"),
        )
        cur.execute("SELECT count(*) FROM doc_pages WHERE source_id = %s", (source_id,))
        (pages_before,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM doc_chunks WHERE page_id = %s", (page_id,))
        (chunks_before,) = cur.fetchone()
    assert pages_before == 1
    assert chunks_before == 1

    sources_repo.delete_source(db_conn, source_id)

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM doc_pages WHERE source_id = %s", (source_id,))
        (pages_after,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM doc_chunks WHERE page_id = %s", (page_id,))
        (chunks_after,) = cur.fetchone()
    assert pages_after == 0
    assert chunks_after == 0
