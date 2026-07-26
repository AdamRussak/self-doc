# Runbook — self-docs

Operational procedures for the self-hosted MCP docs pipeline. Assumes you are
in the repo root with a populated `.env` (see `.env.example`).

---

## REQUIRED — apply the `doc_sources` config migration (read this first)

**Source of truth for crawl config moved from `ingestion/config/sources.yaml`
to the `doc_sources` table in Postgres.** `doc_sources` gained the full
crawl-config columns — `sitemap`, `include_prefixes`, `exclude_prefixes`,
`max_pages`, `language`, `rate_limit_rps`, `schedule_cron`, `enabled`,
`status`, `proposed_by`, `created_at` — via `db/init/02_sources_config.sql`.
**This reverses previously-documented guidance in this runbook**: editing
`sources.yaml` no longer takes effect on the next `/sync` (see the corrected
"Add a new doc source" section below). `ingestion/config/sources.yaml` has
since been **deleted from the repo entirely** (`ingestion/config/` now holds
only a `.gitkeep` so the Docker build still has a directory to bind-mount).
`sources_repo.import_from_yaml` still exists and is still tested, but
nothing in the codebase calls it — it is a programmatic-only helper, not a
wired-up boot path, and no env var triggers it at container start.

**Update (ADR-003): `02_sources_config.sql` also carries the llms.txt /
conditional-GET / multilingual-FTS columns.** The same file now additionally
adds (all idempotent `ADD COLUMN IF NOT EXISTS`, same as the columns above):
- `doc_sources.llms_txt` — `TEXT NOT NULL DEFAULT 'auto'`, constrained by
  `doc_sources_llms_txt_check` to `'auto' | 'off' | 'only'`.
- `doc_sources.llms_etag`, `doc_sources.llms_last_modified` — conditional-GET
  validators for the llms.txt index fetch itself.
- `doc_pages.etag`, `doc_pages.last_modified` — per-page conditional-GET
  validators (see [HTTP conditional skip](#http-conditional-skip-etag--if-modified-since)
  below).
- `doc_chunks.fts_config` — `regconfig NOT NULL DEFAULT 'english'`, plus a
  **non-idempotent-cost** (though idempotently *guarded*) redefinition of the
  `fts` generated column from a hardcoded `to_tsvector('english', content)`
  to `to_tsvector(fts_config, content)`.

  **This one step is not "just another `ADD COLUMN`" — read before running
  it against a large, live corpus.** Postgres has no `ALTER COLUMN ...`
  form for a generated column's expression, so this migration drops and
  re-adds `doc_chunks.fts`, which **rewrites the entire `doc_chunks` table
  and rebuilds `doc_chunks_fts_idx` (the GIN index)**. This takes an
  `ACCESS EXCLUSIVE`-equivalent lock on `doc_chunks` for the duration and
  its cost scales with corpus size (rows × avg chunk size) — plan a
  **maintenance window** for this specific step on any deployment with a
  non-trivial corpus (the three seed sources are small enough this is
  seconds; a much larger corpus should not assume that). It runs at most
  once — the migration's `DO` block detects the old hardcoded expression via
  `pg_attrdef` and no-ops on every subsequent re-run, including accidental
  ones. On a **fresh volume** (nuke-and-rebuild path, ADR-002) this cost
  never applies: `01_schema.sql` creates `fts_config`/`fts` correctly from
  first init.
- See ADR-003 (`docs/adr/003-llms-txt-etag-multilang-fts.md`) for the full
  design rationale behind all three of these additions.

`db/init/*.sql` scripts run **only** against an empty Postgres data
directory (first cluster init). On any **existing** database — which is the
case for this deployment — `02_sources_config.sql` must be applied by hand:

```bash
set -a; source .env; set +a
./scripts/migrate.sh
```

`scripts/migrate.sh` runs `psql -v ON_ERROR_STOP=1` against the running
`self-docs-db` container with `02_sources_config.sql`. It is **idempotent**
(every statement is `ADD COLUMN IF NOT EXISTS` or a guarded `DO` block for
the `CHECK` constraint / the `fts` redefinition above) — safe to re-run at
any time, including by accident. `.github/workflows/test.yml` applies both
`01_schema.sql` and `02_sources_config.sql` when building the CI database, so
CI exercises the same live-migration path documented here, not just the
fresh-volume path.

**Status: this migration has already been applied to this deployment's live
database.** Nobody needs to (and nobody should assume they still need to) run
it against the current production Postgres instance. Document/run it only
when:
- standing up a **second instance** from scratch on an existing (non-empty)
  data directory carried over from before this change, or
- rebuilding via the nuke-and-rebuild path documented below onto a **fresh**
  volume, where `db/init/*.sql` (including this file) already runs
  automatically and re-running by hand is a redundant no-op, not a required
  step.

---

## Pre-built Container Images (GHCR)

Pre-built, multi-architecture container images (`linux/amd64` and `linux/arm64`) for both services are automatically published to GitHub Container Registry (GHCR) via CI (`.github/workflows/release.yml`) on every release tag (`v*.*.*`) and update to `main`.

**Package URLs (`<owner>` must be lowercase):**
- `ghcr.io/<owner>/self-docs-ingestion:latest` (or specific tag like `:v1.0.0` / `:main`)
- `ghcr.io/<owner>/self-docs-mcp-server:latest`

**Consuming via Docker Compose:**
If you prefer pulling pre-built images instead of building locally (`docker compose build`), override `build:` in your compose configuration or add `image:` references:

```yaml
services:
  ingestion:
    image: ghcr.io/<owner>/self-docs-ingestion:latest
  mcp-server:
    image: ghcr.io/<owner>/self-docs-mcp-server:latest
```

*Note: The pre-built GHCR images come pre-baked with the default embedding model (`mixedbread-ai/mxbai-embed-large-v1`). If you switch to a custom model via `make configure MODEL=...`, you must build from source so the new ONNX model weights are downloaded during container build.*

**Authentication (if pulling from private registry):**
```bash
echo "$GITHUB_TOKEN" | docker login ghcr.io -u "$GITHUB_USERNAME" --password-stdin
```
A GitHub Personal Access Token (PAT) with `read:packages` scope is required when pulling private GHCR packages.

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
   queries. **This is unverified in-repo** — `doc_sources` does not carry an
   indexed Traefik documentation source, so this repo cannot cite
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

## Rotate `SYNC_TOKEN`

`SYNC_TOKEN` is **one shared bearer secret guarding every privileged surface
on the `ingestion` service**:

| Surface | Effect if the token leaks |
| --- | --- |
| `POST /sync`, `POST /refresh` | Attacker triggers arbitrary crawls (CPU, bandwidth, upstream rate-limit bans) |
| `POST /purge` | **Destructive** — deletes indexed pages |
| `GET/POST /admin/*` (login) | Full CRUD over `doc_sources`, including `POST /admin/sources/{id}/delete` |

Rotate it when: it was ever set to a placeholder (`change-me` and friends —
see below); it was pasted into a chat/issue/log/screenshot; a laptop or
`.env` backup with it went missing; an admin **session cookie** may have
leaked (the cookie is a deterministic function of `SYNC_TOKEN`, so rotation is
the *only* revocation mechanism — see "Admin UI" below); or an operator with
access left. Otherwise, on a routine schedule.

### Is my token a placeholder?

Since the boot-time check landed, `ingestion` inspects `SYNC_TOKEN` at import
time, before uvicorn binds a socket:

- **Any non-loopback listener declared in `SELF_DOCS_LISTENERS` + a
  placeholder token → the container refuses to start**, printing a `FATAL`
  line to stderr that names the offending listener and the fix. This is the
  production case: `docker-compose.prod.yml` sets `SELF_DOCS_LISTENERS` to its
  Traefik hostname, so a Traefik-exposed deployment cannot boot on `change-me`.
- **Loopback-only + a placeholder token → it boots but prints a `WARNING`**,
  so a working local `make up` is never broken by this check. Find it with:

  ```bash
  docker compose logs ingestion | grep -i 'placeholder'
  ```

The placeholder list lives in `ingestion/app/security.py`
(`_PLACEHOLDER_TOKENS` / `_PLACEHOLDER_PREFIXES`) and covers `change-me`,
`changeme`, `REPLACE_ME_*`, `secret`, `password`, quoting/case/underscore
variants, and similar. It is an exact/prefix match list, **not** an entropy
check: a short-but-real token is accepted, so passing the check is not the
same as having a *strong* token. Always generate.

### Procedure

Budget a short window: steps 3–4 break every client that still sends the old
token, and step 5 logs every admin session out.

1. **Generate a strong token.** 32 bytes of CSPRNG output, hex-encoded:

   ```bash
   openssl rand -hex 32
   ```

   Equivalent if `openssl` is unavailable:

   ```bash
   python3 -c 'import secrets; print(secrets.token_hex(32))'
   ```

   Do not hand-write one, do not reuse `MCP_TOKEN`, and do not reuse a token
   from another service. Keep it out of shell history — on `zsh`/`bash`, a
   leading space with `HIST_IGNORE_SPACE`/`HISTCONTROL=ignorespace` set, or
   pipe it straight into your password manager.

2. **Update `.env` on the Docker host** (the only authoritative copy;
   `.env` is gitignored and must stay that way):

   ```
   SYNC_TOKEN=<the generated value>
   ```

   Nothing else in the repo needs editing — `docker-compose.yml` interpolates
   `SYNC_TOKEN: ${SYNC_TOKEN}` from `.env`.

3. **Update every consumer that stores the value.** `SYNC_TOKEN` is the
   *ingestion/admin* token, so its blast radius is narrower than `MCP_TOKEN`,
   but check all of these:

   - **Humans logging into `/admin`** — the login form takes the raw
     `SYNC_TOKEN`. Tell whoever uses it; a stale value just fails to log in.
   - **`doc-cli`** — reads `API_TOKEN` / `SYNC_TOKEN` / `MCP_TOKEN` from the
     environment or `--token` (`cli/cmd/root.go`). Update whatever exports it
     (shell profile, direnv `.envrc`, systemd unit, CI secret).
   - **`scripts/push_sources.py`** — takes `--token` or `$SYNC_TOKEN`.
   - **`make sync`** — reads `SYNC_TOKEN` from `.env` automatically, so it
     picks up the new value with no extra action.
   - **Any cron/CI job or monitoring probe** that curls `/sync`, `/refresh`,
     `/purge`, or `/status` with an `Authorization: Bearer` header.
   - **MCP client configs (`docs/client-setup.md`)** — these send
     **`MCP_TOKEN`**, not `SYNC_TOKEN`, and are therefore *unaffected* by this
     rotation. If you are rotating `MCP_TOKEN` instead (or both), every client
     in `docs/client-setup.md` (Claude Code, Cursor, Antigravity, …) must have
     its `Authorization: Bearer <MCP_TOKEN>` header updated, and they should be
     updated **before** the container restart to avoid a window of `401`s.

4. **Restart `ingestion` to pick up the new environment.** A restart is
   required — the value is read once at import time, so `docker compose
   restart` on a container whose env was changed in `.env` is not enough;
   recreate it:

   ```bash
   docker compose up -d ingestion
   # production (Traefik overlay):
   # docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile full up -d ingestion
   ```

5. **Know that rotation invalidates every existing admin session.** The
   `/admin` session cookie and the CSRF token are
   `HMAC-SHA256(SYNC_TOKEN, "session-v1")` / `…"csrf-v1"` — deterministic
   functions of the token, with no server-side session store. Changing
   `SYNC_TOKEN` changes both, so:

   - Every logged-in browser is logged out at the next request and must
     re-login at `/admin/login` with the **new** token.
   - Any in-flight admin form POST will fail CSRF validation; redo it after
     re-login.
   - This is exactly why rotation is the remediation for a leaked admin
     cookie: there is no other revocation path.

6. **Verify.** The old token must be rejected and the new one accepted:

   ```bash
   # expect 401
   curl -s -o /dev/null -w '%{http_code}\n' \
     -H "Authorization: Bearer <the OLD value>" \
     http://127.0.0.1:8080/sync -X POST

   # expect 200/202/409 (409 = a sync is already running; still proves auth passed)
   curl -s -o /dev/null -w '%{http_code}\n' \
     -H "Authorization: Bearer $SYNC_TOKEN" \
     http://127.0.0.1:8080/sync -X POST
   ```

   And confirm the container came up clean, with no placeholder warning:

   ```bash
   docker compose ps ingestion
   docker compose logs --tail=50 ingestion | grep -iE 'FATAL|WARNING|placeholder'
   ```

7. **Destroy the old value.** Delete it from your password manager's active
   entry (keep it only in that entry's history if you need an audit trail),
   from shell history, and from any scratch file. If it was ever committed to
   git, rotating is necessary but **not sufficient** — the value stays in the
   history for anyone with a clone; treat that as a separate incident.

### `SELF_DOCS_LISTENERS`

The boot check cannot see its own exposure — inside the container uvicorn
always binds `0.0.0.0:8080`, and reachability is decided by the compose
`ports:` mapping and by any reverse proxy. So the compose layer declares it:

| File | Value | Placeholder token behavior |
| --- | --- | --- |
| `docker-compose.yml` | `127.0.0.1:8080` | warn, boots |
| `docker-compose.prod.yml` | `127.0.0.1:8080,https://${DOCS_MCP_HOSTNAME}/api/v1` | **refuses to boot** |
| unset | defaults to `127.0.0.1` | warn, boots |

**If you expose `ingestion` by any means other than the prod overlay** — your
own nginx/caddy, an extra published port, a VPN interface — add that address
to `SELF_DOCS_LISTENERS` in `.env`, or the check will keep only warning while
the service is in fact reachable. The unset default is permissive on purpose
(it must not break existing loopback-only installs), which means this check is
**advisory for hand-rolled ingress and enforcing only where exposure is
declared**.

---

## Add a new doc source

**`doc_sources` in Postgres is the sole source of truth for crawl config.**
`ingestion/config/sources.yaml` has been **deleted from the repo**
(`ingestion/config/` now holds only a `.gitkeep` so the Docker build still
has a directory to bind-mount). `sources_repo.import_from_yaml` still exists
and is still tested, but nothing in the codebase calls it — it is a
programmatic-only helper, not a wired-up boot path; no env var imports a
YAML file at container start. (Superseded guidance, corrected here: an
earlier revision of this runbook said editing `sources.yaml` took effect on
the next `/sync` — that has not been true since sources moved into
Postgres.) A source is added via the admin UI, the MCP `propose_doc_source`
tool, or `scripts/push_sources.py`.

There are two ways to add a source, human (admin UI) and agent (MCP
proposal):

### A. Human: the admin UI

1. Open `http://127.0.0.1:8080/admin/login` (loopback-only — see
   [Admin UI](#admin-ui) below for exposure/auth details) and log in with
   `SYNC_TOKEN`.
2. **Sources → New source.** `base_url` is the only truly required field —
   creation is **URL-only capable**: leave `name`, `include_prefixes`, and
   `max_pages` blank and the server derives safe defaults for you
   (`ingestion/app/source_defaults.py`):
   - `name` — a `^[a-z0-9-]+$` slug derived from the host + first
     meaningful path segment (e.g. `doc.traefik.io` → `traefik`,
     `developers.google.com/maps` → `google-maps`), with a `-2`/`-3`/...
     suffix on collision against every existing name (including
     `rejected` ones).
   - `include_prefixes` — `[base_url's path]` when `base_url` has a
     non-root path (scoping the crawl to that path), or `[]` (whole host)
     for a root URL.
   - `max_pages` — defaults to `500` (`DEFAULT_MAX_PAGES`) when omitted,
     so a bare URL on a root path can never crawl an entire shared docs
     host uncapped with no ceiling at all.

   Any field you *do* supply always wins over its derived default —
   including explicitly passing an empty `include_prefixes` (whole host) or
   an explicit "no limit" `max_pages`. Other fields: `sitemap` (optional;
   BFS fallback if absent), `exclude_prefixes` (one per line), `language`
   (default `english`), `rate_limit_rps` (default `1.0`), `llms_txt`
   (default `auto`). Validation errors re-render the form with the exact
   problem (duplicate name — more likely now that names are auto-derived
   from `base_url` — bad `base_url`, a sitemap-less source whose `base_url`
   isn't covered by its own `include_prefixes`, an unsupported `language`)
   — nothing is written until it passes.

   - **`llms_txt` mode (`auto` | `off` | `only`, default `auto`).** Controls
     whether the crawler prefers a source's [llms.txt](https://llmstxt.org)
     index over the normal HTML sitemap/BFS crawl. `auto` tries
     `{base_url origin}/llms-full.txt` then `/llms.txt`; if either is found,
     it indexes that pre-cleaned markdown (split into per-section pages)
     instead of crawling HTML, and falls back to the normal HTML crawl if
     neither exists. `off` disables the llms.txt lookup entirely (the prior,
     only behavior). `only` uses the llms.txt content if found and indexes
     **nothing** for that source if it isn't — no HTML fallback. See
     `docs/adr/003-llms-txt-etag-multilang-fts.md` for the design rationale.
     **Changing `llms_txt` on an existing source triggers a full re-index of
     that source on its next sync** (the set of indexed URLs changes), bound
     by the existing purge-ratio/coverage guards — expected, not a bug.
   - **`language`** must be one of the ~30 Postgres built-in text-search
     configuration names in `SUPPORTED_FTS_LANGUAGES`
     (`ingestion/app/config.py`) — e.g. `english`, `french`, `german`,
     `spanish`, `simple`, etc. This drives `doc_chunks.fts_config`, which in
     turn drives the language passed to `to_tsvector`/`websearch_to_tsquery`
     for that source's chunks at both index and search time. An unsupported
     value is rejected at save time with the full allowed list in the error,
     not left to fail later at query time.
3. A source created this way lands with `status='active'` immediately (the
   human creating it via an authenticated admin session is itself the
   approval). Use the source's **Sync** button (or `POST /sync
   {"source": "my-new-source"}`, see the [migration
   note](#migration-note-post-sync-changes) below) to trigger its first
   crawl, then poll `GET /status` as before:

   ```bash
   curl -sS http://localhost:8080/status | jq '."my-new-source"'
   ```

   *(Note: A source reporting `last_status: "ok"` with a small number of
   `pages_soft_failed > 0` is healthy and normal — it indicates a few
   expected real-world site quirks like 404/503 links or stub pages. But a
   **high ratio** of soft failures relative to total pages seen — above
   20% — degrades the status to `"partial"` even with zero hard failures,
   by design. See [Page Classification & Source Status
   Semantics](#page-classification--source-status-semantics) below.)*

4. Optionally set a `schedule_cron` on the source's edit form to have it
   sync automatically — see [The scheduler](#the-scheduler) below for the
   supported cron subset and the `SCHEDULER_ENABLED` opt-in.

### B. Agent: `propose_doc_source` (MCP tool)

An AI agent with `search_docs`/`list_doc_sources` access can also call the
MCP tool `propose_doc_source(base_url, name?, max_pages?, sitemap?,
include_prefixes?, exclude_prefixes?, language?, rate_limit_rps?)`. Only
`base_url` is required — `name`, `include_prefixes`, and `max_pages` are
derived the same way as the URL-only admin-UI path above
(`source_defaults.apply_creation_defaults`) when omitted, so a bare URL can
never propose crawling an entire shared docs host uncapped. This **never**
crawls anything directly:

- It validates the same `SourceConfig` fields as the admin form and, on
  success, inserts a row with `status='pending'`.
- `proposed_by` records a **truncated SHA-256 hash of the caller's bearer
  token** (`sources_repo.derive_proposed_by`) — never the raw token — so an
  operator can tell "was this the same agent/token as that other proposal"
  without the admin UI ever displaying a live credential.
- A `pending` source is **uncrawlable**: `/sync` refuses it with `403`
  whether targeted directly, by name in a `sources` list, or swept up in an
  unscoped "sync everything" call (see the [migration
  note](#migration-note-post-sync-changes) below) — until a human approves
  it.

**Approval workflow:** open the admin UI (`/admin`) — pending proposals are
listed separately from active sources. Review the proposal (name, URL,
prefixes, `proposed_by`), then:
- **Approve** (`POST /admin/sources/{id}/approve`) → `status='active'`,
  crawlable from then on.
- **Reject** (`POST /admin/sources/{id}/reject`) → `status='rejected'`,
  permanently excluded from "sync all active sources" and from being
  targeted by name/id on `/sync` (403) until manually re-approved.

No source proposed via MCP is ever crawled without this explicit,
human-in-the-loop admin-UI step.

---

## Admin UI

Server-rendered CRUD UI over `doc_sources`, mounted at `/admin` on the
`ingestion` service.

**Exposure: loopback only on the base compose file, by design.**
`docker-compose.yml` publishes `ingestion` as `127.0.0.1:8080:8080` — bound to
the Docker host's loopback interface, not `0.0.0.0`. This is a **deliberate
security property, not an oversight**: the admin UI can create/edit/delete
crawl targets and trigger crawls, so on `make up` it is reachable only from
the Docker host itself (SSH tunnel or sitting at the box). Do not publish it
on `0.0.0.0` as a "convenience" without re-running the security review.

> **Open discrepancy — verify on your own deployment before trusting the
> paragraph above.** This section previously claimed there is "no Traefik
> router for `ingestion`". That is **not true of `docker-compose.prod.yml`**,
> which attaches `ingestion` to the external Traefik network and declares
> `Host(${DOCS_MCP_HOSTNAME}) && PathPrefix(/api/v1)` with rate-limit
> middleware. Today no application route lives under `/api/v1` (the app serves
> `/sync`, `/admin`, … at the root), so `/admin` is not believed to be
> reachable through that router — but that is an accident of path prefixes,
> not an enforced boundary, and it would silently stop holding the moment
> anyone mounts the app under `/api/v1`, adds a `stripprefix` middleware, or
> broadens the rule. If you run the prod overlay, confirm from **off-box**
> that `https://<DOCS_MCP_HOSTNAME>/admin/login` and
> `https://<DOCS_MCP_HOSTNAME>/api/v1/purge` are not served, and treat this as
> an open item rather than a settled one.

**Auth.** `GET /admin/login` renders a form; paste `SYNC_TOKEN` (the same
token `POST /sync` already requires) into it. On success you get an
`httponly`, `SameSite=Lax` session cookie scoped to `path=/admin`. Every
state-changing (POST) route additionally requires a hidden CSRF token
rendered into the form.

**Full CRUD + workflow surface:**
- Create/edit/delete a source (same `SourceConfig` fields as the removed
  `sources.yaml` schema, plus `schedule_cron` and `enabled`).
- Manual per-source sync button (`POST /admin/sources/{id}/sync`) — refuses
  a non-`active` source with a clear message ("approve it first") rather
  than a bare error.
- Approve/reject pending MCP proposals (see above).

**Known limitation — read this before treating a leaked admin cookie as
low-severity.** Both the session cookie value and the CSRF token are
**deterministic functions of `SYNC_TOKEN`** (`HMAC-SHA256(SYNC_TOKEN,
"session-v1")` / `"csrf-v1"`), not per-login random nonces — there is no
server-side session store. This means:
- Every login produces the *same* cookie/CSRF pair until `SYNC_TOKEN`
  changes.
- **Rotating `SYNC_TOKEN` is the only way to revoke a leaked admin session
  cookie.** There is no per-session logout/revoke; if a cookie is captured
  (browser history, a shared log line, XSS on some other page sharing the
  browser profile), it remains valid indefinitely until you rotate the
  token. Treat a suspected admin-cookie leak exactly like a suspected
  `SYNC_TOKEN` leak: rotate `SYNC_TOKEN` in `.env` and restart `ingestion` —
  full procedure in "Rotate `SYNC_TOKEN`" above.

---

## The scheduler

**Opt-in, per-source.** Each source has its own `schedule_cron` column
(`NULL` by default — no automatic firing). Set it via the admin UI's edit
form or `sources_repo.set_schedule`.

**`SCHEDULER_ENABLED` defaults to OFF.** Set `SCHEDULER_ENABLED=true` (or
`1`/`yes`) in `.env` to turn the scheduler loop on at all — with it unset or
falsy, the scheduler task never starts, regardless of how many sources have
a `schedule_cron` set.

**Supported cron syntax — a restricted 5-field subset, not full POSIX cron.**
A `schedule_cron` value MUST be exactly 5 whitespace-separated fields
(`minute hour day month weekday`, standard field order/ranges: minute
0-59, hour 0-23, day 1-31, month 1-12, weekday 0-6 with 0=Sunday). Each
field must be one of:

| Form | Meaning | Example |
|---|---|---|
| `*` | every value in range | `*` |
| `*/N` | every Nth value starting at the range floor | `*/15` (minute field → every 15 min) |
| a bare integer | exactly that value | `0` |
| a comma-list of bare integers | any of those values | `0,15,30,45` |

**NOT supported — rejected at save time, not silently ignored:** ranges
(`1-5`), step-on-range (`1-10/2`), named values (`MON`, `JAN`), and the
`?`/`L`/`W`/`#` special characters. An operator who writes `1-5` in the
day-of-week field to mean "weekdays" gets the save **refused** with a
`ValueError` naming exactly which field and token was rejected — it is not
silently accepted and then ignored at run time. Express "weekdays" as an
explicit list instead: `0 3 * * 1,2,3,4,5` (03:00, Mon–Fri).

Example — every Sunday at 03:00: `0 3 * * 0`.

**Observability — answering "why didn't source X sync last night?" from
logs alone.** Every scheduling decision the loop makes is a distinct
`structlog` event:

- `fired` — the source was due and its sync completed the trigger call.
- `skipped-not-due` — carries a `reason` field:
  - `disabled` — `enabled=false` on the source.
  - `status=<...>` — not `status='active'` (e.g. `pending`, `rejected`).
  - `no-schedule` — `schedule_cron` is `NULL`.
  - `cron-not-due` — has a schedule, but it doesn't match the current
    minute.
  - `already-fired-this-window` — already fired for this minute-bucket
    (double-fire guard within one poll cycle).
- `skipped-locked` — due, but another sync (manual, `/sync`, or another
  scheduled source) held the shared sync lock at trigger time.
- `errored` — the trigger call itself raised; the source stays eligible
  next poll.

```bash
docker compose logs ingestion | grep '"source": "my-new-source"' | grep -E '"event": "(fired|skipped-not-due|skipped-locked|errored)"'
```

Read the `reason` field on a `skipped-not-due` line to get the exact answer
— e.g. `reason: "status='pending'"` means the source was never approved,
`reason: "no-schedule"` means nobody ever set `schedule_cron` on it,
`reason: "cron-not-due"` means the schedule simply didn't match that
minute.

---

## Migration note: `POST /sync` API changes

The `/sync` endpoint accepts `{"sources": [names]}` or no body to sync
all configured sources. Additionally:

- **NEW:** accepts `{"source": id|name}` for single-source sync (used by
  the admin UI's manual-sync button) — `int` targets `doc_sources.id`,
  `str` targets `doc_sources.name`. Mutually exclusive with the existing
  `{"sources": [names]}`; if both are somehow sent, `source` wins.
- **CHANGED:** a database read failure now returns **`503`** (previously a
  `sources.yaml` config error returned `400` — there is no longer a
  `sources.yaml` config error path at request time at all, since the DB is
  the source of truth).
- **CHANGED:** "sync all" (no `source`/`sources` in the body) now means
  **"sync all `status='active'` sources"** — `pending` and `rejected`
  sources are excluded from an unscoped sync.
- A source targeted by id/name whose `status != 'active'` is refused with
  **`403`** (approve it first). An unknown id or name on the single-source
  (`source`) path is **`404`**; an unknown name in the list (`sources`) path
  remains **`400`** (unchanged, matches the existing contract for that
  path).

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

## Switch the embedding model

The embedding model is selected from a registry (`config/models.yaml`, the
single source of truth). Selecting a model auto-derives its vector dimension and
the two services' Docker memory limits. The default is
`mixedbread-ai/mxbai-embed-large-v1` (1024-dim). To see the options:

```bash
grep -E '^  [A-Za-z]' config/models.yaml   # the model keys under `models:`
```

Switching models changes the vectors AND (usually) the `vector(N)` column width,
so it requires re-rendering the schema, rebuilding the images (the model is
baked in at build time), and a full re-embed. Because change-detection
(content-hash) skips unchanged pages, an in-place re-sync is not enough — the
corpus must be truncated and re-embedded.

```bash
# 1. Select the model: writes EMBEDDING_* + *_MEM_LIMIT into .env and renders
#    db/init/01_schema.sql to the new vector(N). No MODEL => the registry default.
make configure MODEL=intfloat/multilingual-e5-large

# 2. Rebuild images so the new model is pre-baked, and recreate the DB schema.
#    If the vector dimension changed, the column must be recreated — the
#    simplest correct path is the nuke-and-rebuild above:
docker compose build ingestion mcp-server
docker compose down -v db && docker compose up -d db   # re-runs the rendered schema
docker compose ps db                                   # wait for healthy
docker compose up -d                                   # (make up)

# 3. Re-embed the corpus. If you kept the DB (same dimension), use `make reindex`
#    instead of the nuke above; it truncates doc_pages/doc_chunks and re-syncs:
make reindex

# 4. Verify quality held/improved against your eval set:
make eval
```

`make configure` requires PyYAML on the machine running it (`pip install pyyaml`,
or use the ingestion venv: `ingestion/.venv/bin/python scripts/configure_model.py <model>`).
The ingestion and mcp-server services MUST run the same model — `make configure`
keeps both in sync via `.env`, and a startup dimension mismatch surfaces as a
pgvector error on the first embed/search.

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
# Weekly backup, Mondays at 04:00
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

## Isolated test database (db-test)

`make test` never runs against the production `db` service/volume. DB-backed
tests (ingestion's `test_store.py`/`test_sources_repo.py`/etc,
mcp-server's `test_retrieval_integration.py`, and the cross-package
`tests/test_e2e.py`) need a live Postgres, and they exercise purge/delete code
paths — running them against the real index would risk deleting real rows
mid-suite.

**The fix: a wholly separate `db-test` service**, defined in
`docker-compose.test.yml`:
- Its own container (`self-docs-db-test`), its own named volume
  (`pgdata_test`) — never the production `pgdata` volume.
- `db/init/` is bind-mounted the same way as production, so the schema
  (including the `vector(N)` dimension rendered by `make configure`) always
  matches what the tests expect.
- Published on `127.0.0.1:5433` — the same host/port the test suites'
  `os.environ.setdefault("POSTGRES_PORT", "5433")` fallbacks already expect,
  so no test code needed to change.
- Fixed, non-production credentials (`self_docs` / `testpass123` /
  `self_docs`), matching those same test-suite fallbacks. These credentials
  are never used for a real deployment and are not secrets.

The production `db` service in `docker-compose.yml` publishes **no host
port** — it is reachable only inside the compose network
(`self-docs-internal`), by container DNS (`db:5432`), matching security
review finding M1. `docker-compose.test.yml` does not touch or extend the
`db` service at all; `db-test` is a fully independent service definition.

`make test` brings `db-test` up automatically (`test-db-up` target, waited
until its healthcheck reports `healthy`) before running any suite, and pins
`EMBEDDING_DIM=384 EMBEDDING_MODEL_NAME=BAAI/bge-small-en-v1.5` for every
test invocation — matching the deployed default in `config/models.yaml` and
the `vector(384)` column in `db/init/01_schema.sql`. Without this, DB-backed
tests fail on `expected 384 dimensions, not 1024` because
`ingestion/app/embedder.py`'s hardcoded fallback constants
(`DEFAULT_MODEL_NAME`/`DEFAULT_EMBEDDING_DIM`) still default to the 1024-dim
`mxbai` model — a known, deliberately-untouched mismatch (see
`test_registry_defaults.py::test_retrieval_defaults_match_registry_default`
and its ingestion-side twin in `tests/test_model_registry.py`, both
intentionally left red). Do not "fix" this by editing
`mcp-server/app/retrieval.py` or `config/models.yaml` — the Makefile
env-pinning is the correct workaround, not those fallback constants.

**Useful targets:**

```bash
make test-db-up      # start db-test (own container+volume), wait for healthy
make test-db-down     # stop the db-test container, keep its data
make test-db-reset    # stop db-test AND delete pgdata_test — next test-db-up
                      # re-applies db/init/ from scratch on an empty volume
```

`db-test` is fully disposable: `make test-db-reset` (or manually `docker
compose -f docker-compose.yml -f docker-compose.test.yml down -v db-test`)
wipes it completely with zero risk to the production `pgdata` volume, since
they are entirely separate named volumes.

**A concurrent process holding `127.0.0.1:5433` on the host** (e.g. a stale
container from an old, since-removed shape of `docker-compose.test.yml` that
republished the production `db` service's port instead of using a separate
`db-test` service) will make `docker compose up -d db-test` fail with "port
is already allocated". Find and stop whatever is bound to that port
(`docker ps` / `lsof -iTCP:5433 -sTCP:LISTEN`) — it should never be the
production `db` container once every deployment/worktree is running the
current compose files, since `db` no longer publishes a port at all.

---

## Expected sync durations

Driven by each source's `max_pages` and `rate_limit_rps` (`doc_sources`
columns — see the admin UI for the live values; `ingestion/config/sources.yaml`
no longer exists in the repo) (crawler etiquette: ~1 req/sec per source,
sequential fetch):

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
therefore on the order of 20–40 minutes.

---

## HTTP conditional skip (ETag / If-Modified-Since)

`doc_pages` stores `etag`/`last_modified` from each page's most recent
successful fetch. On a re-sync, the crawler sends `If-None-Match`/
`If-Modified-Since` for any URL that has a previously-recorded validator; an
upstream `304 Not Modified` response skips download *and* markdown
extraction entirely for that page — a stronger short-circuit than the
existing content-hash skip (`pages_skipped`), which still required a full
fetch+extract before comparing hashes.

- **New Prometheus counter: `pages_not_modified_total{source="..."}`.**
  Counts pages skipped via a `304` on a given source. Distinct from
  `pages_skipped_total` (hash-diff match after a full fetch) — a source
  whose origin supports conditional GET should show most of its steady-state
  re-syncs landing in `pages_not_modified_total` rather than
  `pages_fetched_total`.

  ```bash
  curl -sS http://localhost:8080/metrics | grep -E "^pages_(not_modified|skipped|fetched)_total"
  ```

- **`GET /status` also reports `pages_not_modified`** per source, alongside
  the existing `pages_fetched`/`pages_skipped`/`pages_failed`/
  `pages_soft_failed` counts.
- **Not every origin supports conditional GET.** A source whose responses
  never carry `ETag`/`Last-Modified` will simply never populate
  `pages_not_modified` — its pages fall back to the existing full-fetch +
  content-hash path (`pages_skipped` on repeat, unchanged syncs). This is
  expected, not a misconfiguration.
- `doc_sources.llms_etag`/`llms_last_modified` carry the same validators for
  a source's llms.txt index fetch. The read/write plumbing exists today; a
  `304` short-circuit for the whole-index fetch (skipping the parse step,
  not just per-page fetches) is a documented future enhancement, not yet
  wired into the sync path — see `docs/adr/003-llms-txt-etag-multilang-fts.md`.

---

## Page Classification & Source Status Semantics

The ingestion pipeline separates transient, expected site quirks (`pages_soft_failed`) from actionable internal defects (`pages_failed`) so operational alarms and status checks remain high-signal. **`classify_sync` in `ingestion/app/store.py` is the single source of truth for how these counters (plus a few out-of-band signals) turn into a `last_status` of `ok`/`partial`/`failed` — this section summarizes it; read the function's docstring for the exact, ordered rule list.**

### Three-Tier Page Classification

1. **`pages_soft_failed` (Expected Site Quirks & Transient Skips)**
   Pages that encountered expected real-world site friction during crawling or content extraction:
   - **Stale/Broken Links (`fetch_ok=False`)**: Upstream sitemaps or HTML navigation links pointing to dead `404`/`503` URLs, or pages blocked by `robots.txt`. These URLs are added to `seen_urls` (so `_delete_missing_pages()` does not prematurely purge legitimate existing rows when a page is temporarily unreachable) and logged as `page_fetch_skipped`.
   - **Stub / Placeholder Pages (`extraction.status != "ok"`)**: Pages with very little or malformed content (e.g., `<200` characters of Markdown or empty shells after boilerplate stripping) that are skipped during extraction and logged as `page_content_skipped`.
   - *Behavior:* A **small number** of soft failures does not degrade a source's overall status — they're recorded for observability. But a soft-failure **ratio** above `SOFT_FAIL_PARTIAL_RATIO` (20% of all pages seen) does degrade status to `"partial"`, even with zero hard failures — see "Source Status Determination" below for why this changed.

2. **`pages_skipped` (Unchanged Hash Matches)**
   Pages whose content SHA-256 hash exactly matches existing database rows from a previous sync. These are skipped instantly without re-chunking or re-embedding (`page_unchanged_skip`).

3. **`pages_failed` (Actionable Pipeline Defects)**
   Pages that encountered real, actionable internal errors during processing (e.g., database connection drops, transaction errors inside `replace_page()`, or `chunker.chunk_markdown()` crashes). These represent genuine infrastructure or pipeline failures that require operator intervention.

### Source Status Determination (`last_status`)

**This section previously documented the pre-2026-07-26 rules, which had a real defect: a 40-source production run reported `last_status="ok"` for every single source — including 6 that indexed zero pages, and `traefik`, which silently lost 117 of 280 pages (42%) to soft failures while still reading `"ok"`.** The root cause was an implicit conditional chain where `pages_soft_failed` counted toward both "was anything processed" (defeating the empty-crawl guard) and "did this succeed" (making any nonzero soft-failure count look like success as long as the *hard*-failure counter was zero).

`classify_sync` replaced that implicit chain with an explicit, ordered, unit-tested set of rules (`ingestion/app/store.py`, evaluated top to bottom, first match wins):

1. **`"failed"`** — the sync was cancelled by the user, or nothing was indexed or confirmed this run at all (`pages_fetched + pages_skipped + pages_not_modified == 0`) — a source where every page soft/hard-failed, or the crawl saw nothing, must never read as `"ok"`.
2. **`"partial"`** — the purge-ratio guard refused to delete anything (itself worth surfacing), OR the crawl was aborted early / a sitemap-cap truncation cut discovery short (either way, the corpus is known-incomplete), OR at least one **hard** pipeline failure occurred (`pages_failed > 0`), OR the **soft-failure ratio** exceeds `SOFT_FAIL_PARTIAL_RATIO` (`pages_soft_failed / pages_seen > 0.2`, where `pages_seen` sums every category: fetched, skipped, hard-failed, soft-failed, not-modified).
3. **`"ok"`** — otherwise.

**Why a *ratio* threshold, and why this is a deliberate reversal, not a bug fix:** the original intent — don't let expected site quirks (a few dead links, a stub page) degrade status and make alarms noisy — was sound and is preserved: a source with a couple of incidental soft failures out of hundreds of pages still reports `"ok"`. What was missing was any upper bound at all: a source silently losing almost half its content to soft failures (traefik's 117/280 = 41.8%) is not an "incidental quirk," it's a systematic loss that operators need paged on. A ratio floor (20%, chosen with comfortable margin below the traefik incident and above normal single-digit soft-fail noise) catches the systematic case while staying silent on the incidental one — see `SOFT_FAIL_PARTIAL_RATIO`'s comment in `ingestion/app/store.py` for the exact calibration.

### Observability & Signals

You can monitor page outcomes across three operational interfaces:

#### 1. Querying `GET /status`
The JSON status payload exposes exact counts for each classification per source:
```bash
curl -sS http://localhost:8080/status | jq '."traefik"'
```
Example output for a healthy source with a few transient broken links/stubs (`pages_soft_failed > 0`, but well under the 20% ratio threshold — 5/163 ≈ 3%):
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
The `/metrics` endpoint exposes counters/gauges for each outcome tier (`ingestion/app/metrics.py` is the single definition point for all of these):
```bash
curl -sS http://localhost:8080/metrics | grep -E "^(pages_(fetched|skipped_unchanged|not_modified|soft_failed|failed)_total|sync_last_status|sync_last_success_timestamp)"
```
Relevant series:
- `pages_fetched_total{source="..."}`
- `pages_skipped_unchanged_total{source="..."}`
- `pages_not_modified_total{source="..."}`
- `pages_soft_failed_total{source="..."}`
- `pages_failed_total{source="..."}` — hard pipeline failures; the counter that drives `partial`/`failed` alongside the soft-fail ratio.
- `chunks_indexed_total{source="..."}`
- `sync_duration_seconds{source="..."}` (histogram)
- `sync_last_success_timestamp{source="..."}` — set to "now" for BOTH `ok` and `partial` (only `failed` leaves it stale; see [Alerting](#alerting--prometheus-rules-opsalertsingestionyml) below).
- `sync_last_status{source="...", status="ok"|"partial"|"failed"}` — a labelled gauge, `1` for exactly one status per source at a time (the other two status series are removed, not merely zeroed, on every recorded sync — see `metrics.py`'s module docstring on the "stale series" pitfall this avoids). A **direct** read of `classify_sync`'s verdict, not a proxy computed from other series.

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

- **Is the embedding model available offline?** Yes — the configured model is
  pre-downloaded into both the `ingestion` and `mcp-server` images at build time
  via the `EMBEDDING_MODEL_NAME` build arg (see each Dockerfile), which
  docker-compose feeds from `.env`. No network access is needed at runtime for
  embedding; a fresh container start does not re-download the model. Note this
  means **changing the model requires rebuilding both images** so the new
  weights are baked in — see [Switch the embedding model](#switch-the-embedding-model).

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
  curl http://localhost:8080/metrics         # pages_fetched_total, pages_not_modified_total, chunks_indexed_total, ...
  curl http://mcp-server:8000/metrics        # from inside the compose network — search_requests_total, search_latency_seconds
  ```

  Neither service publishes ports to the host by default (see
  `docker-compose.yml`); reach them from another container on the
  `self-docs-internal` network, or temporarily add an uncommitted compose
  override to publish a port for local debugging.

- **`409` on `POST /sync`.** A sync is already running — one shared lock now
  covers `POST /sync`, the admin UI's manual-sync button, and the
  scheduler, so any of the three can be the reason another is blocked. Not
  an error — wait and poll `GET /status`, or treat it as a no-op (this is
  how the scheduler's `skipped-locked` log event handles it too).

- **`503` on `POST /sync`.** The database read failed (Postgres
  unreachable, connection error, ...) — see the [migration
  note](#migration-note-post-sync-changes) above; this replaces what used
  to be a `400` back when `sources.yaml` was the config source. The service
  itself stays up; retry once the DB is reachable again.

- **`403` on `POST /sync`.** The targeted source (by id, by name, or swept
  into an unscoped sync) has `status != 'active'` — most commonly
  `pending` (an MCP proposal awaiting approval) or `rejected`. Approve it
  in the admin UI first (see [Add a new doc
  source](#add-a-new-doc-source) above).

- **`404` on `POST /sync`.** Only on the single-source `{"source": id|name}`
  path — the id/name doesn't exist in `doc_sources`. (An unknown name in
  the `{"sources": [names]}` list form still returns `400`, unchanged.)

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
  Remember that under our three-tier classification, a **small number** of transient dead links (`404`/`503`) or short stub pages recorded in `pages_soft_failed` do not, by themselves, trigger `partial` status. But if a source reports `partial` or `failed`, it indicates one of: real hard errors (`pages_failed > 0`), an empty/unconfirmed crawl (nothing fetched, skipped, or not-modified), an aborted/truncated crawl, a refused purge guard, or a soft-failure **ratio** above 20% of pages seen (see [Page Classification & Source Status Semantics](#page-classification--source-status-semantics) above for the exact ordered rules in `classify_sync`). Check `docker compose logs ingestion` for `sync_source_crashed` or `page_index_failed` events — usually caused by database connectivity loss, transaction exceptions, or an over-restrictive prefix filter/dead sitemap resulting in zero discovered pages. Fix `include_prefixes`/`exclude_prefixes`/`sitemap` on the source's admin UI edit form (`doc_sources` — no longer `ingestion/config/sources.yaml`, see the migration note at the top of this runbook) and re-sync; other sources are unaffected by one source's failure.

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
  happened with the `traefik` source (its original sitemap URL 404d). If a
  source goes quiet, check whether its declared `sitemap` still 200s before
  looking anywhere else.

  **Triage path (this is the failure this whole alerting program exists to
  close — a log investigation once found all 40 sources reporting
  `last_status="ok"`, including 6 holding zero pages and one silently
  down 42% of its content).** Don't rely on eyeballing `last_status` alone —
  it reads `"ok"` in exactly this scenario. Work through these in order:

  1. **Did `SourceIndexedNothing` fire?** (`ops/alerts/ingestion.yml`, see
     [Alerting](#alerting--prometheus-rules-opsalertsingestionyml) below).
     If so, a completed sync run produced zero pages across
     `pages_fetched_total` + `pages_skipped_unchanged_total` +
     `pages_not_modified_total` for that `source` label — that is the exact
     condition described above. Confirm by hand:
     ```bash
     curl -sS http://localhost:8080/metrics | grep -E '^(pages_fetched|pages_skipped_unchanged|pages_not_modified)_total\{source="<name>"'
     ```
     All three at (or unchanged from) their pre-incident values, alongside a
     freshly-bumped `sync_duration_seconds_count{source="<name>"}`, confirms
     "ran, indexed nothing."
  2. **Check `GET /status` for the source** to confirm `last_status` and
     `pages_seen`:
     ```bash
     curl -sS http://localhost:8080/status | jq '."<name>"'
     ```
  3. **Check whether the source's `sitemap` still 200s** (the traefik root
     cause):
     ```bash
     curl -sS -o /dev/null -w '%{http_code}\n' '<the source'"'"'s sitemap URL>'
     ```
     A non-`200` here, on a source that also declares `include_prefixes` not
     covering its own `base_url`, is the trap: config validation passed
     (the sitemap-set BFS-seed-filter short-circuit), but a dead sitemap
     falls back to a BFS crawl seeded on `base_url` that its own
     `include_prefixes` immediately filters to zero. Fix the sitemap URL (or
     the `include_prefixes`) on the source's admin UI edit form and re-sync.
  4. **If the sitemap is fine**, check `docker compose logs ingestion` for
     `page_fetch_skipped`/`page_content_skipped` volume on that source (soft
     failures — see `SoftFailRatioHigh` below) versus a genuinely empty
     crawl (`pages_seen == 0` in the logs' `sync_source` summary) to tell
     apart "the whole crawl found nothing" from "the crawl found pages but
     they were all soft-failed."

---

## Alerting — Prometheus rules (`ops/alerts/ingestion.yml`)

Five alerting rules over the `/metrics` series from
`ingestion/app/metrics.py` (`pages_fetched_total`,
`pages_skipped_unchanged_total`, `pages_not_modified_total`,
`pages_soft_failed_total`, `pages_failed_total`, `chunks_indexed_total`,
`sync_duration_seconds`, `sync_last_success_timestamp`, and
`sync_last_status` — the last one labelled `source` + `status`, the rest
labelled `source` only). Load them into
Prometheus via the usual `rule_files:` entry in `prometheus.yml` (not wired
into this repo's compose files yet — add
`ops/alerts/ingestion.yml` to your Prometheus instance's `rule_files:` to
activate them). Every rule fires per-`source` label — none of them enumerate
source names, since sources are added/removed at runtime via the admin UI /
MCP proposal flow.

Lint/test locally:

```bash
promtool check rules ops/alerts/ingestion.yml
```

### Alert: SourceSyncStale

Fires when `time() - sync_last_success_timestamp{source=...}` exceeds **48h**
(`for: 30m`). Syncs run ~daily and take ~3.5h for the full source set, so 48h
tolerates one missed/late run without paging on schedule jitter alone, while
still catching "hasn't synced in 2+ days."

**Triage:**
1. `docker compose logs ingestion | grep '"source": "<name>"' | grep -E '"event": "(fired|skipped-not-due|skipped-locked|errored)"'` —
   read the scheduler's `reason` field (see [The scheduler](#the-scheduler)
   above) to see whether it's simply not scheduled (`no-schedule`), disabled,
   or actually erroring.
2. `curl -sS http://localhost:8080/status | jq '."<name>"'` — check
   `last_synced`/`last_status`/`error`.
3. If the source has genuinely never had a successful sync (no
   `sync_last_success_timestamp` series at all — this alert can't detect
   that case; see the rule file's own comment on why absence and staleness
   are handled distinctly), trigger a manual sync (`POST /sync
   {"source": "<name>"}` or the admin UI's Sync button) and watch the logs
   for `sync_source_crashed`/`page_index_failed`.

### Alert: SourceSyncDegraded

Fires when `sync_last_status{status="partial"} == 1` for a source (`for:
15m`) — a **direct** read of `classify_sync`'s verdict for that source's most
recently completed sync, not a proxy: `ingestion/app/metrics.py`'s
`record_sync_outcome` sets `sync_last_status{source, status}` to 1 for the
current status and removes the other two status label values for that
source, so at most one status series is active per source at any time (see
`metrics.py`'s module docstring for why a labelled gauge, and how the
classic "stale series" pitfall is avoided). `partial` means at least one of:
a hard pipeline failure (`pages_failed > 0`), an early-aborted crawl, a
refused purge-ratio guard, or a soft-failure ratio above
`SOFT_FAIL_PARTIAL_RATIO` — see `classify_sync` in `ingestion/app/store.py`
for the exact rule order.

**Triage:**
1. `curl -sS http://localhost:8080/status | jq '."<name>"'` — check the real
   `last_status` and `pages_failed` for the last couple of runs.
2. `docker compose logs ingestion | grep '"source": "<name>"' | grep -E '"event": "(page_index_failed|sync_source_crashed)"'` —
   find which specific condition (`classify_sync`'s four `partial` branches)
   applied to this run.
3. Also rule out `SourceIndexedNothing`/sitemap issues (above) — a source
   trending toward zero pages will also trip this alert on its way down.

### Alert: SourceIndexedNothing

Fires when a sync run completes (`sync_duration_seconds_count` increases in
the window) but `pages_fetched_total + pages_skipped_unchanged_total +
pages_not_modified_total == 0` for that run (`for: 10m`, `severity:
critical`). This is the headline failure mode this whole alert suite exists
to close — see the ["A source reports `ok` but `pages_seen == 0`" triage
path](#troubleshooting) above (in Troubleshooting), which this alert is
designed to trigger.

### Alert: SoftFailRatioHigh

Fires when `pages_soft_failed_total` exceeds **20%** of a source's total
page volume in its last run, on a run with at least 20 total pages
(`for: 15m`). 20% is set comfortably below the `traefik` incident that
motivated this rule (117 of 280 pages, 41.8%, soft-failed while, at the
time, still reporting `"ok"` — this is the exact ratio `classify_sync`'s
`SOFT_FAIL_PARTIAL_RATIO` rule now demotes to `"partial"`, see [Page
Classification & Source Status Semantics](#page-classification--source-status-semantics)
above) so it would have caught that incident, while staying well above the
low single-digit soft-fail rates a healthy source normally shows from
ordinary dead links/stub pages. The `> 20` total-page floor avoids alerting
on statistically meaningless ratios from tiny sources (e.g. 1 soft-failed
page out of 3 total).

**Triage:** `curl -sS http://localhost:8080/status | jq '."<name>"'` to see
`pages_soft_failed` alongside `pages_fetched`, then `docker compose logs
ingestion | grep '"source": "<name>"' | grep -E '"event": "page_(content|fetch)_skipped"'`
to see which URLs are soft-failing and why (dead link vs. stub content) —
same as the general soft-failure guidance under [Page Classification & Source
Status Semantics](#page-classification--source-status-semantics).

### Alert: NoSyncMetricsAtAll

`severity: critical` meta-alert. Fires when `pages_fetched_total` is
`absent()` for over **4h** (`for: 4h`, chosen to exceed the ~3.5h full
sync-set duration so a normal container restart — where the metric family is
legitimately absent until the first sync completes and recreates it — does
not page). This is the regression guard for the exact original incident: a
prior version of `/metrics` emitted zero application samples because the
counters were only incremented from a route handler no real sync went
through. If this fires, every other rule in this file is blind at the same
time — treat it as the highest-priority alert in the file.

**Triage:**
1. `curl -sS http://localhost:8080/metrics | grep pages_fetched_total` — confirm
   the series is genuinely absent, not just filtered by your query.
2. Confirm at least one sync has actually completed recently
   (`docker compose logs ingestion | grep sync_source_crashed` — is
   everything crashing before `record_sync_outcome` runs?) versus the
   metrics-recording path itself being broken again (check
   `ingestion/app/store.py`'s `sync_source_with_metrics` is still the entry
   point every sync route calls, per its own docstring).
3. Restart `ingestion` (`docker compose up -d ingestion`) only after
   confirming it isn't simply mid-first-sync since a fresh deploy.
