# ADR-002: Nuke-and-Rebuild for Schema Evolution

**Status:** Accepted  
**Date:** 2026-07-18  
**Decision makers:** Project owner + architect (pre-dispatch review)

## Context

The database schema (`doc_sources`, `doc_pages`, `doc_chunks`) may evolve as we
add features — e.g., per-source FTS language configs would require changing the
`fts` generated column definition on `doc_chunks`.

Standard approaches for schema evolution in Python/Postgres projects include:

1. **Alembic** — SQLAlchemy's migration tool; versioned migration scripts.
2. **Numbered SQL init scripts** — `db/init/01_schema.sql`, `02_*.sql`, etc.
3. **Nuke-and-rebuild** — drop the volume, re-init, re-sync.

## Decision

Use **nuke-and-rebuild** as the schema evolution strategy for the MVP:

```bash
docker compose down -v db     # drops pgdata volume
docker compose up -d db       # re-runs db/init/*.sql on fresh volume
make sync                     # full re-crawl of all sources
```

Numbered init scripts (`db/init/01_schema.sql`, `02_*.sql`, …) run in order on
a fresh volume only. No migration tool is deployed.

## Rationale

The corpus is **fully re-crawlable** — every chunk in the database can be
reconstructed by re-crawling the upstream documentation sites. There is no
user-generated or non-rebuildable data in the schema. This makes the cost of a
volume wipe essentially zero (a ~30-minute re-sync of all sources).

Alembic adds:
- A dependency (`alembic`, `sqlalchemy`)
- An `alembic/` directory with version files
- Operational complexity (running migrations, handling failed migrations)

None of this is justified when the data is ephemeral and re-crawlable.

## Consequences

- **Positive:** Zero migration tooling to maintain; schema changes are just
  editing `01_schema.sql` and re-syncing.
- **Positive:** No risk of failed migrations leaving the DB in an inconsistent
  state.
- **Negative:** All indexed data is lost on every schema change — a full
  re-sync is required (20-40 minutes for the seed sources).
- **Negative:** If the DB ever accumulates non-rebuildable state (e.g.,
  user-curated relevance scores, manual chunk annotations), this strategy
  breaks. **Adopt Alembic at that point** (recorded for future reference).

## Related

- `db/init/01_schema.sql` — the authoritative schema
- `docs/runbook.md` § "Re-index from scratch" — the documented procedure
