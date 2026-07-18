# Runbook — self-docs

Operational procedures for the self-hosted MCP docs pipeline. Assumes you are
in the repo root with a populated `.env` (see `.env.example`).

---

## Add a new doc source

1. Edit `ingestion/app/sources.yaml` and add an entry per the schema (see
   `IMPLEMENTATION_PLAN.md` §2 "sources.yaml schema"):

   ```yaml
   - name: my-new-source        # unique, [a-z0-9-]
     base_url: https://example.com/docs/
     sitemap: https://example.com/sitemap.xml   # optional; BFS fallback if absent
     include_prefixes: ["/docs/"]                # optional allowlist
     exclude_prefixes: ["/blog/"]                 # optional denylist (wins over include)
     max_pages: 300                               # required hard cap
     language: english                            # optional, default english
     rate_limit_rps: 1.0                          # optional, default 1.0
   ```

   Validation is fail-fast at ingestion startup (pydantic): duplicate names,
   missing/invalid `base_url`, or unknown keys abort the service. Test your
   YAML by restarting the ingestion container and checking it comes up:

   ```bash
   docker compose restart ingestion
   docker compose logs ingestion --tail=50
   ```

2. Get the change live — the ingestion service only reads `sources.yaml` at
   startup, so either:
   - **Rebuild + restart** (needed if you changed the file inside the image
     build context and want it baked in for reproducibility):
     ```bash
     docker compose build ingestion
     docker compose up -d ingestion
     ```
   - **Restart only** (fastest path for local iteration — `sources.yaml` is
     read from disk at process start, a plain restart re-reads it as long as
     the file is present in the built image/mount):
     ```bash
     docker compose restart ingestion
     ```

3. Trigger a sync of just the new source:

   ```bash
   make sync
   ```

   (`make sync` syncs all sources; to target one, call `/sync` directly)

   ```bash
   curl -sS -X POST http://localhost:8080/sync \
     -H "Authorization: Bearer $SYNC_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"sources": ["my-new-source"]}'
   ```

4. Poll `GET /status` until `running: false`, then confirm
   `last_status: "ok"` and `chunks_indexed > 0` for the new source:

   ```bash
   curl -sS http://localhost:8080/status | jq '."my-new-source"'
   ```

   *(Note: A source reporting `last_status: "ok"` with `pages_soft_failed > 0` is completely healthy and normal — it indicates expected real-world site quirks like 404/503 links or stub pages. Only `pages_failed > 0` triggers `"partial"` or `"failed"`. See [Page Classification & Source Status Semantics](#page-classification--source-status-semantics) below.)*

---

## Re-index from scratch (nuke-and-rebuild)

Schema evolution and "start clean" both use this path — the corpus is fully
re-crawlable, so there is no migration tool for the MVP.

```bash
docker compose down -v db      # drops the pgdata volume — all indexed data is lost
docker compose up -d db        # re-runs db/init/*.sql on the fresh volume
# wait for db to report healthy:
docker compose ps db
docker compose up -d           # (or: make up) bring up ingestion + mcp-server
make sync                      # full sync of every seed source
```

`down -v db` only targets the `db` service's volumes — it does not touch
`ingestion`/`mcp-server` containers or images. Watch `docker compose logs db`
for the init scripts (`db/init/01_schema.sql`, …) running in order; confirm
with `\dx` (pgvector extension) and `\dt` (three tables) via
`docker compose exec db psql -U $POSTGRES_USER -d $POSTGRES_DB`.

---

## Backup

### Manual

```bash
make backup
```

Runs `pg_dump -Fc` inside the `db` container and writes a timestamped
custom-format archive to `./backups/docs_<timestamp>.dump` on the host. Safe
to run at any time (MVCC — a sync in progress does not block or corrupt the
backup).

To prune old backups (keeping the 4 most recent by default):

```bash
make backup-prune          # keep 4
make backup-prune KEEP=7   # keep 7
```

Or run both in one step:

```bash
make backup-auto           # backup + prune (keeps 4)
```

### Automated (cron)

Use `scripts/backup.sh` with cron or a systemd timer. The script validates
that the `db` container is running before attempting a backup.

```bash
# Weekly backup, Mondays at 04:00 (day after the n8n sync)
# Add to crontab: crontab -e
0 4 * * 1 /path/to/self-docs/scripts/backup.sh >> /var/log/self-docs-backup.log 2>&1
```

Environment variables:
- `SELF_DOCS_DIR` — path to the repo root (default: auto-detected from script location)
- `KEEP` — number of backups to retain (default: 4)

## Restore

```bash
make restore FILE=backups/docs_20260101_030000.dump
```

This runs `pg_restore --clean --if-exists` against the live `db` container,
dropping and recreating the dumped objects in place.

**After a large restore**, rebuild the HNSW index — `pg_dump` preserves the
index *definition* but not a pre-built structure, and `pg_restore` runtime
increases with corpus size:

```bash
docker compose exec db psql -U $POSTGRES_USER -d $POSTGRES_DB \
  -c "REINDEX INDEX doc_chunks_embedding_idx;"
```

**Alternative to restoring at all:** since the corpus is fully re-crawlable
from upstream doc sites, it is usually simpler and just as fast to skip
restore entirely and re-sync from scratch (`make sync` after a nuke-and-
rebuild, above) rather than restoring an old dump — a restore is only worth
it if upstream sources have since changed or gone away and you want to
recover the point-in-time index.

---

## Expected sync durations

Driven by `max_pages` and `rate_limit_rps` in `ingestion/app/sources.yaml`
(crawler etiquette: ~1 req/sec per source, sequential fetch):

| Source            | `max_pages` | Rough duration            |
|--------------------|------------:|----------------------------|
| `pgvector-readme`  | 3           | < 1 minute                 |
| `fastapi`          | 500         | ~10–20 minutes             |
| `nextjs`           | 500         | ~10–20 minutes             |

These are rough (a few hundred pages actually fetched in practice — many
URLs get filtered by `include_prefixes`/`exclude_prefixes` before counting
against the cap; unchanged pages on repeat syncs are skipped almost
instantly via hash-diff, so weekly re-syncs are much faster than the first
full crawl). A full first-time sync of all three seed sources together is
therefore on the order of 20–40 minutes; budget the n8n poll timeout
(default 60 min, see `docs/n8n/docs-sync.json`) accordingly if you add
larger sources.

---

## Page Classification & Source Status Semantics

The ingestion pipeline separates transient, expected site quirks (`pages_soft_failed`) from actionable internal defects (`pages_failed`) so operational alarms and status checks remain high-signal.

### Three-Tier Page Classification

1. **`pages_soft_failed` (Expected Site Quirks & Transient Skips)**
   Pages that encountered expected real-world site friction during crawling or content extraction:
   - **Stale/Broken Links (`fetch_ok=False`)**: Upstream sitemaps or HTML navigation links pointing to dead `404`/`503` URLs, or pages blocked by `robots.txt`. These URLs are added to `seen_urls` (so `_delete_missing_pages()` does not prematurely purge legitimate existing rows when a page is temporarily unreachable) and logged as `page_fetch_skipped`.
   - **Stub / Placeholder Pages (`extraction.status != "ok"`)**: Pages with very little or malformed content (e.g., `<200` characters of Markdown or empty shells after boilerplate stripping) that are skipped during extraction and logged as `page_content_skipped`.
   - *Behavior:* Soft failures do **not** degrade a source's overall status. They are recorded for observability but do not trigger operational alerts or `"partial"` statuses.

2. **`pages_skipped` (Unchanged Hash Matches)**
   Pages whose content SHA-256 hash exactly matches existing database rows from a previous sync. These are skipped instantly without re-chunking or re-embedding (`page_unchanged_skip`).

3. **`pages_failed` (Actionable Pipeline Defects)**
   Pages that encountered real, actionable internal errors during processing (e.g., database connection drops, transaction errors inside `replace_page()`, or `chunker.chunk_markdown()` crashes). These represent genuine infrastructure or pipeline failures that require operator intervention.

### Source Status Determination (`last_status`)

At the conclusion of `sync_source()`, the source's overall `last_status` is assigned:
- **`"ok"`**: Zero hard errors occurred (`pages_failed == 0`) AND at least one page was processed (`pages_fetched + pages_skipped + pages_soft_failed > 0`). A source with `pages_soft_failed > 0` and `pages_failed == 0` correctly reports `"ok"`.
- **`"partial"`**: At least one hard error occurred (`pages_failed > 0`), but one or more pages in the source succeeded or skipped (`succeeded_any` is true).
- **`"failed"`**: Every attempted page encountered a hard pipeline error (`pages_failed > 0` and `succeeded_any` is false), OR the crawl yielded zero pages (`pages_seen == 0`, e.g., dead sitemap URL or over-restrictive `include_prefixes`).

### Observability & Signals

You can monitor page outcomes across three operational interfaces:

#### 1. Querying `GET /status`
The JSON status payload exposes exact counts for each classification per source:
```bash
curl -sS http://localhost:8080/status | jq '."traefik"'
```
Example output for a healthy source with transient broken links/stubs (`pages_soft_failed > 0`):
```json
{
  "pages_fetched": 158,
  "pages_skipped": 0,
  "pages_failed": 0,
  "pages_soft_failed": 5,
  "pages_removed": 0,
  "chunks_indexed": 1504,
  "last_status": "ok",
  "last_synced": 1752872160.123,
  "error": null
}
```

#### 2. Checking Prometheus Metrics
The `/metrics` endpoint exposes counters for each outcome tier:
```bash
curl -sS http://localhost:8080/metrics | grep -E "^pages_(fetched|skipped|soft_failed|failed)_total"
```
Relevant series:
- `pages_fetched_total{source="..."}`
- `pages_skipped_total{source="..."}`
- `pages_soft_failed_total{source="..."}`
- `pages_failed_total{source="..."}`

#### 3. Filtering Structured JSON Logs (`structlog`)
Every page classification logs a distinct, structured JSON event:
```bash
# Watch for soft failures (broken upstream links or stub content skips)
docker compose logs ingestion | grep -E '"event": "page_(content|fetch)_skipped"'

# Watch for real actionable pipeline exceptions or source crashes
docker compose logs ingestion | grep -E '"event": "(page_index_failed|sync_source_crashed)"'
```

---

## Troubleshooting

- **Is the embedding model available offline?** Yes — `BAAI/bge-small-en-v1.5`
  is pre-downloaded into both the `ingestion` and `mcp-server` images at
  build time (see each Dockerfile). No network access is needed at runtime
  for embedding; a fresh container start does not re-download the model.

- **Reading logs.** Both services emit structured JSON lines to stdout via
  `structlog` (fields: `ts`, `level`, `service`, `event`, plus context like
  `source`, `url`, `duration_ms`):

  ```bash
  docker compose logs -f ingestion
  docker compose logs -f mcp-server
  docker compose logs ingestion --since 1h | grep sync_source_crashed
  ```

- **Checking health/metrics.**

  ```bash
  curl http://localhost:8080/health          # ingestion liveness (if published locally)
  curl http://localhost:8080/metrics         # pages_fetched_total, chunks_indexed_total, ...
  curl http://mcp-server:8000/metrics        # from inside the compose network — search_requests_total, search_latency_seconds
  ```

  Neither service publishes ports to the host by default (see
  `docker-compose.yml`); reach them from another container on the
  `self-docs-internal` network, or temporarily add an uncommitted compose
  override to publish a port for local debugging.

- **`409` on `POST /sync`.** A sync is already running (the endpoint is
  guarded by a lock). Not an error — wait and poll `GET /status`, or treat it
  as a no-op (this is exactly how the n8n workflow handles it).

- **`401` on `POST /sync`.** Missing or wrong `Authorization: Bearer
  $SYNC_TOKEN` header. Confirm the token matches `.env`'s `SYNC_TOKEN` — the
  ingestion container also refuses to start entirely if `SYNC_TOKEN` is
  unset, so a `401` means the service is up but the caller sent the wrong
  token, not that auth is misconfigured server-side.

- **Empty `heading_path` on GitHub-README-derived sources** (e.g.
  `pgvector-readme`). Known, cosmetic quirk — READMEs don't always parse
  into a clean heading breadcrumb the way a docs site's nested pages do. The
  chunk content and source URL are still correct and citable; this does not
  indicate a broken sync.

- **A source keeps coming back `partial` or `failed`.**
  Remember that under our three-tier classification, transient dead links (`404`/`503`) or short stub pages are recorded in `pages_soft_failed` and do **not** trigger `partial` status. If a source reports `partial` or `failed`, it indicates real hard errors (`pages_failed > 0`) or an empty crawl (`pages_seen == 0`). Check `docker compose logs ingestion` for `sync_source_crashed` or `page_index_failed` events — usually caused by database connectivity loss, transaction exceptions, or an over-restrictive prefix filter/dead sitemap resulting in zero discovered pages. Fix `include_prefixes`/`exclude_prefixes`/`sitemap` in `sources.yaml` and re-sync; other sources are unaffected by one source's failure.
