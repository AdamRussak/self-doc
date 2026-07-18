# Runbook — self-docs

Operational procedures for the self-hosted MCP docs pipeline. Assumes you are
in the repo root with a populated `.env` (see `.env.example`).

---

## Deploy / Upgrade — MCP_TOKEN requirement (read before restarting mcp-server)

This applies to any deploy/upgrade that brings the `mcp-server` image up to a
version that enforces `MCP_TOKEN` auth on `/mcp` (see the `401` troubleshooting
entry below for the auth behavior itself — this section is the pre-deploy
checklist that prevents the failure mode in the first place).

1. **Add `MCP_TOKEN` to `.env` before deploying.** `.env.example` documents the
   variable, but an existing `.env` created before this change will not have
   it. Generate one:

   ```bash
   openssl rand -hex 32
   ```

   Add it to `.env` as `MCP_TOKEN=<generated-value>` alongside `SYNC_TOKEN`.

   **Failure mode if skipped:** `docker-compose.yml` interpolates
   `MCP_TOKEN: ${MCP_TOKEN}` into the `mcp-server` service environment. If
   `MCP_TOKEN` is unset in `.env`, this interpolates to an empty string. The
   server's startup fail-fast check treats an empty `MCP_TOKEN` the same as a
   missing one and refuses to start, so the container exits `1` immediately
   and Docker's restart policy brings it back up into the same failure —
   a **restart loop**. If you see `mcp-server` repeatedly restarting/exiting
   right after this upgrade, or `docker compose logs mcp-server` showing an
   immediate exit with no requests ever served, this is almost certainly the
   cause — check `.env` for `MCP_TOKEN` first. Confirm by running
   `docker compose logs mcp-server` and grepping for the literal line the
   server prints to stderr before exiting:

   ```
   FATAL: MCP_TOKEN environment variable is required but not set. Refusing to start.
   ```

2. **This is a breaking change for every existing MCP client.** Once
   `mcp-server` enforces `MCP_TOKEN`, any client (Cursor, Claude Code,
   Antigravity, etc.) still configured without an `Authorization: Bearer
   <MCP_TOKEN>` header will start getting `401` on every tool call the moment
   the new `mcp-server` container is up — see `docs/client-setup.md` for the
   exact header shape each client needs.

   **Safe ordering:**
   1. Add `MCP_TOKEN` to `.env` (step 1 above).
   2. Update every registered client's config to send the `Authorization`
      header (`docs/client-setup.md`).
   3. Rebuild/restart the `mcp-server` container:
      ```bash
      docker compose up -d --build mcp-server
      ```

   Doing the client updates *before or alongside* the container restart avoids
   a window where clients are silently broken with `401`s.

3. **Post-deploy: verify the Traefik `serversTransport` binding (production
   only).** `docker-compose.prod.yml` declares a Traefik `serversTransport`
   label intended to raise the backend timeout for slow embedding+pgvector
   queries. **This is unverified in-repo** — `ingestion/config/sources.yaml` does
   not index a Traefik documentation source, so this repo cannot cite
   `search_docs` evidence that the Traefik v3 Docker provider actually builds
   `http.serversTransports.*` from container labels the way the compose file
   assumes. Confirm it manually after deploying:

   ```bash
   curl <traefik-api>/api/http/serversTransports
   ```

   Confirm `self-docs-mcp-transport@docker` exists in the output **and** is
   bound to the `mcp-server` service. If it does not appear, the intended 60s
   backend timeout is **not** in effect, and the underlying 504 risk on slow
   embedding+pgvector queries remains open — treat that as a follow-up, not a
   silent no-op.

---

## Add a new doc source

1. Edit `ingestion/config/sources.yaml` and add an entry per the schema (see
   `IMPLEMENTATION_PLAN.md` §2 "sources.yaml schema"):

   ```yaml
   - name: my-new-source        # unique, [a-z0-9-]
     base_url: https://example.com/docs/
     sitemap: https://example.com/sitemap.xml   # optional; BFS fallback if absent
     include_prefixes: ["/docs/"]                # optional allowlist
     exclude_prefixes: ["/blog/"]                 # optional denylist (wins over include)
     max_pages: 300                               # REQUIRED — no default (see below)
     language: english                            # optional, default english
     rate_limit_rps: 1.0                          # optional, default 1.0
   ```

   `max_pages` is **required with no default** — omitting it fails config
   validation. This is not a hypothetical: it's the exact mistake that once
   made an edit to this file silently have no effect at all (the file
   failed validation, so the last-known-good config kept serving instead).
   Before syncing, validate the file directly with the same loader the
   service uses:

   ```bash
   cd ingestion && ./.venv/bin/python -c "from app.config import load_sources; load_sources('config/sources.yaml')"
   ```

   A clean exit (no output) means the file is valid; a `ConfigError`
   traceback tells you exactly what's wrong (duplicate name, bad
   `base_url`, unknown key, missing `max_pages`, or a sitemap-less source
   whose `base_url` isn't covered by its own `include_prefixes`) before you
   ever hit `/sync`.

2. Get the change live. `ingestion/config/sources.yaml` is bind-mounted
   read-only into the container (`./ingestion/config:/config:ro`,
   `SOURCES_YAML=/config/sources.yaml`) and is **re-read from disk on every
   `/sync` request** — no rebuild, no restart, just save the file and call
   `/sync`. There are two distinct failure modes to understand, because they
   behave very differently:

   - **Container startup (fail-fast).** `sources.yaml` is also loaded once
     at process start, before uvicorn binds. If it's invalid at that point,
     the container prints `FATAL: invalid sources.yaml (...)` to stderr and
     exits non-zero — it will **not** boot. This only bites you after a
     restart/recreate of the `ingestion` container (e.g. `docker compose up
     -d ingestion`, or a host reboot), not from editing the file while the
     service is already running.
   - **Runtime re-read on `/sync` (fail-soft).** If you edit the file while
     `ingestion` is already running and introduce an error, the *next*
     `/sync` call re-reads it, gets a `ConfigError`, and returns **HTTP
     400** with the validation message in the response body — the service
     itself keeps running unaffected, still serving the previous
     last-known-good config for any other request. Nothing crashes; you
     just don't get your new/changed source until the file is fixed.

   In short: a bad edit that's only ever read at runtime degrades gracefully
   (400, old config keeps working); a bad edit that's present when the
   container itself starts up prevents it from coming up at all. Use the
   pre-sync validation command above to avoid hitting either path.

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

Driven by `max_pages` and `rate_limit_rps` in `ingestion/config/sources.yaml`
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

- **`401` on `POST /mcp` (or any tool call).** Missing or wrong
  `Authorization: Bearer $MCP_TOKEN` header. Confirm the client's configured
  token matches `.env`'s `MCP_TOKEN` on the `mcp-server` container — see
  `docs/client-setup.md` for the exact header shape each client needs. As
  with `SYNC_TOKEN`, a `401` here means the service is up and reachable but
  the caller sent a missing/incorrect token, not that auth is misconfigured
  server-side. Note that `GET /metrics` is intentionally left unauthenticated
  on both `mcp-server` and `ingestion` so the Docker healthcheck and
  Prometheus can scrape it without a token.

  See also [Deploy / Upgrade — MCP_TOKEN
  requirement](#deploy--upgrade--mcp_token-requirement-read-before-restarting-mcp-server)
  above if you are hitting `401`s (or a restart loop) right after upgrading
  `mcp-server` — that section is the pre-deploy checklist for exactly this.

- **Empty `heading_path` on GitHub-README-derived sources** (e.g.
  `pgvector-readme`). Known, cosmetic quirk — READMEs don't always parse
  into a clean heading breadcrumb the way a docs site's nested pages do. The
  chunk content and source URL are still correct and citable; this does not
  indicate a broken sync.

- **A source keeps coming back `partial` or `failed`.**
  Remember that under our three-tier classification, transient dead links (`404`/`503`) or short stub pages are recorded in `pages_soft_failed` and do **not** trigger `partial` status. If a source reports `partial` or `failed`, it indicates real hard errors (`pages_failed > 0`) or an empty crawl (`pages_seen == 0`). Check `docker compose logs ingestion` for `sync_source_crashed` or `page_index_failed` events — usually caused by database connectivity loss, transaction exceptions, or an over-restrictive prefix filter/dead sitemap resulting in zero discovered pages. Fix `include_prefixes`/`exclude_prefixes`/`sitemap` in `ingestion/config/sources.yaml` and re-sync; other sources are unaffected by one source's failure.

  **A source reports `ok` but `pages_seen == 0` (silently indexed nothing)**,
  or a previously-healthy source suddenly goes empty after a sitemap moves.
  Watch for this specific trap: a source whose `base_url` path is *not*
  covered by its own `include_prefixes` only passes config validation
  because it also declares a `sitemap` — the validator's
  BFS-seed-filter check (`config.py`'s `_base_url_passes_own_prefix_filters`)
  short-circuits and skips the check entirely whenever `sitemap` is set. If
  that sitemap URL ever 404s (moves, gets renamed upstream, etc.), the
  crawler falls back to BFS seeded on `base_url` — which `include_prefixes`
  then filters out immediately, before the first fetch — and the source
  syncs "successfully" with zero pages indexed. This is exactly what
  happened with the `traefik` source (its original sitemap URL 404d) and is
  why the `mcp` source in `ingestion/config/sources.yaml` carries an inline
  `WARNING:` comment about this same shape. If a source goes quiet, check
  whether its declared `sitemap` still 200s before looking anywhere else.
