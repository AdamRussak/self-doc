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

```bash
make backup
```

Runs `pg_dump -Fc` inside the `db` container and writes a timestamped
custom-format archive to `./backups/docs_<timestamp>.dump` on the host. Safe
to run at any time (MVCC — a sync in progress does not block or corrupt the
backup). No automated scheduling in the MVP; run this manually or wire it
into cron/n8n yourself.

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

- **A source keeps coming back `partial` or `failed`.** Check
  `docker compose logs ingestion` for that source's `sync_source_crashed` or
  extraction-length-sanity-check warnings — usually an upstream DOM change
  or a dead sitemap. Fix `include_prefixes`/`exclude_prefixes`/`sitemap` in
  `sources.yaml` and re-sync; other sources are unaffected by one source's
  failure.
