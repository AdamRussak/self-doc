# Self-Hosted MCP Docs RAG Pipeline — Implementation Document

> A containerized, self-hosted "Cursor `@docs`"-style system: crawl static documentation sites,
> embed them locally, and serve semantic search to local LLM agents (Cursor, Claude Code,
> Antigravity) over the Model Context Protocol. Static reference knowledge lives here; dynamic
> project state stays in Mem0.

**Status:** Design approved for implementation · **Date:** 2026-07-18 · **Rev 2** (post architect review — see §7 for review resolutions)

---

## 1. Research Findings & Validation of the Original Plan

The proposed stack was validated against current (mid-2026) ecosystem state. Summary of what
holds, and what should be adjusted:

| Original choice | Verdict | Notes |
|---|---|---|
| PostgreSQL 16 + pgvector | ✅ Keep, pin ≥0.8.2 | Pin the image to `pgvector/pgvector:0.8.2-pg16` (not the floating `pg16` tag): **CVE-2026-3172** (CVSS 8.1) is a buffer overflow in the parallel HNSW build affecting 0.6.0–0.8.1, fixed in 0.8.2. RDS-compatible as intended. |
| FastEmbed + `BAAI/bge-small-en-v1.5` | ✅ Keep, with prefixes | 384-dim, CPU-friendly ONNX. **Must** use asymmetric prefixes: embed docs via `passage_embed()` and queries via `query_embed()` — FastEmbed applies the BGE `passage:`/`query:` prefixes for you. Skipping this measurably hurts recall. |
| BeautifulSoup crawler | ⚠️ Upgrade | Keep BS4 for link discovery, but add **`trafilatura`** for main-content extraction (strips nav/sidebar/footer boilerplate far better than hand-rolled selectors) and prefer **sitemap.xml** discovery over recursive crawling when available. |
| FastMCP (Python) | ✅ Keep — target **3.x**, pin `fastmcp>=3,<4` | FastMCP 3.0 is GA; **Streamable HTTP** (single `/mcp` endpoint) is the recommended remote transport; legacy SSE is deprecated. Breaking change vs 2.x: `stateless_http` (and host/port) moved off the constructor onto `run()`/`http_app()` — `FastMCP('self-docs')` then `mcp.run(transport="http", host="0.0.0.0", port=8000, stateless_http=True)`. Stateless because a search tool needs no session state and this survives restarts/load-balancing behind Traefik. |
| Traefik routing | ✅ Keep | Route `Host(\`docs-mcp.<lan-domain>\`)` → container port. MCP-over-HTTP is plain HTTP from Traefik's point of view; nothing special needed beyond normal labels (SSE-friendly: no response buffering). |
| n8n weekly sync | ✅ Keep | Ingestion container exposes a small FastAPI trigger endpoint; n8n calls it on a cron and alerts on failure. |
| Mem0 / Docs split via rules files | ✅ Keep | Enforced in `AGENTS.md`/`CLAUDE.md` + tool descriptions themselves (the tool description is the strongest steering surface). |

**Additional decisions from research:**

- **Hybrid search from day one.** Pure vector search on API docs misses exact-token queries
  (function names, CLI flags). Add a `tsvector` column + GIN index and combine BM25-ish
  full-text rank with cosine similarity via Reciprocal Rank Fusion (RRF) in SQL. Cheap to add
  now, painful to retrofit.
- **`vector(384)` plain type is fine** — `halfvec` quantization matters at high dims/large
  corpora; at 384 dims and <1M chunks it's unnecessary complexity. Revisit if the corpus grows.
- **Chunking:** split on markdown heading boundaries first, then window to ~400–600 tokens with
  ~15% overlap. Keep the heading breadcrumb (`Guide > Routing > Dynamic Routes`) and source URL
  in each chunk's metadata — agents need the citation to deep-link.
- **Prior art reviewed** (OpenDocuments, Knowledge-Base-Self-Hosting-Kit, Context7, OpenRAG):
  all validate the architecture (crawl → chunk → embed → pgvector/store → MCP tool). Building
  our own stays justified by the pgvector/RDS requirement, Traefik integration, and the
  Mem0-boundary design; none of the off-the-shelf options give us that combination cleanly.

---

## 2. Architecture

```
                        ┌────────────────────────────────────────────┐
                        │                Docker network               │
  Cursor ──┐            │  ┌─────────┐   ┌──────────────┐            │
  Claude ──┼─ HTTP ──▶  │  │ Traefik │──▶│ FastMCP srv  │──┐         │
  Antigrav ┘  /mcp      │  └─────────┘   │ (search_docs)│  │ SQL     │
                        │        │       └──────────────┘  ▼         │
                        │        │       ┌──────────────┐ ┌────────┐ │
  n8n cron ── webhook ─────────▶ └──────▶│ Ingestion    │▶│ pg16 + │ │
  (weekly + alerts)     │                │ svc (FastAPI)│ │pgvector│ │
                        │                └──────────────┘ └────────┘ │
                        └────────────────────────────────────────────┘
```

**Repository layout:**

```
self-docs/
├── docker-compose.yml
├── .env.example                # committed; .env is gitignored
├── db/
│   └── init/01_schema.sql      # auto-run by postgres entrypoint
├── ingestion/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── app/
│   │   ├── main.py             # FastAPI: POST /sync, GET /health, GET /status
│   │   ├── crawler.py          # sitemap/BFS discovery + fetch (rate-limited)
│   │   ├── extract.py          # trafilatura → markdown
│   │   ├── chunker.py          # heading-aware splitting
│   │   ├── embedder.py         # FastEmbed passage_embed wrapper
│   │   └── store.py            # hash-diff upsert into Postgres
│   └── config/
│       └── sources.yaml        # PHASE 6: historical/seed only — doc_sources
│                               # (Postgres) is now the source of truth for
│                               # crawl config; this file is imported ONLY
│                               # when IMPORT_SOURCES_YAML_ON_BOOT=1 is set
│                               # at container start. See §8.
├── mcp-server/
│   ├── Dockerfile
│   ├── pyproject.toml
│   └── app/
│       ├── server.py           # FastMCP app, streamable-http, stateless
│       └── retrieval.py        # query_embed + hybrid RRF SQL
├── AGENTS.md                   # agent usage rules (Mem0 vs docs split)
├── Makefile                    # up / sync / test / backup / restore recipes (local "CI")
└── docs/
    ├── adr/                    # architecture decision records
    ├── n8n/docs-sync.json      # exported n8n workflow
    ├── client-setup.md
    └── runbook.md
```

**Service ports (canonical):** `mcp-server` listens on **8000** (`/mcp`), `ingestion` listens on
**8080** (`/sync`, `/status`, `/health`, `/metrics`). Neither publishes ports to the host; Traefik
reaches `mcp-server:8000`, n8n reaches `http://ingestion:8080` by container DNS. `db` (5432) is
reachable only inside the compose network.

### Data model

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE doc_sources (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,      -- e.g. "nextjs", matches sources.yaml key
    base_url      TEXT NOT NULL,
    last_synced   TIMESTAMPTZ,
    last_status   TEXT                       -- ok | partial | failed
);

CREATE TABLE doc_pages (
    id            SERIAL PRIMARY KEY,
    source_id     INT NOT NULL REFERENCES doc_sources(id) ON DELETE CASCADE,
    url           TEXT NOT NULL UNIQUE,
    content_hash  CHAR(64) NOT NULL,         -- SHA-256 of extracted markdown
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE doc_chunks (
    id            BIGSERIAL PRIMARY KEY,
    page_id       INT NOT NULL REFERENCES doc_pages(id) ON DELETE CASCADE,
    heading_path  TEXT,                      -- "Guide > Routing > Dynamic Routes"
    chunk_index   INT NOT NULL,
    content       TEXT NOT NULL,             -- markdown
    embedding     vector(384) NOT NULL,
    fts           tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
);

CREATE INDEX doc_chunks_embedding_idx ON doc_chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX doc_chunks_fts_idx ON doc_chunks USING gin (fts);
CREATE INDEX doc_chunks_page_idx ON doc_chunks (page_id);
```

Drift handling: re-crawl → recompute page hash → unchanged pages skipped entirely; changed
pages delete-and-reinsert their chunks in one transaction (`ON DELETE CASCADE` makes this a
single `DELETE FROM doc_pages` + reinsert).

**Schema evolution:** numbered scripts in `db/init/` (`01_schema.sql`, `02_*.sql`, …) run in
order on a fresh volume only. No migration tool for the MVP — the corpus is fully re-crawlable,
so the documented upgrade path is **nuke and rebuild**: `docker compose down -v db`, bring up
with the new init scripts, trigger a full sync. If the DB ever accumulates non-rebuildable
state, adopt Alembic then (recorded as an ADR).

### `ingestion/config/sources.yaml` schema — **superseded as of Phase 6, see §8**

> **This section describes the pre-Phase-6 design and is retained only for
> historical schema reference.** As of Phase 6, `doc_sources` (Postgres) is
> the source of truth for crawl config; `sources.yaml` is a one-way seed
> file imported only when `IMPORT_SOURCES_YAML_ON_BOOT=1` is set at
> container start, and is never re-read on `/sync` or any other request
> path. See §8 "Phase 6 — Sources-in-Postgres, Admin UI, Scheduler, MCP
> Proposals" and `docs/runbook.md` for the current behavior.

Lives at `ingestion/config/sources.yaml`. The field schema below (name,
base_url, sitemap, include_prefixes, exclude_prefixes, max_pages, language,
rate_limit_rps) is unchanged — it's now validated as form input / MCP-tool
input against the same `SourceConfig` pydantic model, on write into
`doc_sources`, instead of as a YAML document re-read on every `/sync`.

```yaml
sources:
  - name: fastapi              # unique, [a-z0-9-], maps to doc_sources.name
    base_url: https://fastapi.tiangolo.com/
    sitemap: https://fastapi.tiangolo.com/sitemap.xml   # optional; BFS fallback if absent
    include_prefixes: ["/tutorial/", "/reference/"]      # optional allowlist
    exclude_prefixes: ["/blog/", "/release-notes/"]      # optional denylist (wins over include)
    max_pages: 500              # REQUIRED, no default — omitting it fails validation
    language: english           # optional, default english → to_tsvector config
    rate_limit_rps: 1.0         # optional, default 1.0 req/sec
```

Ship with three seed sources so the stack is demoable and testable on day one: FastAPI docs,
Next.js docs, and the pgvector README (small, fast to index). Note `language` is per-source but
the `fts` column is a generated column with a fixed config — for the MVP all seed sources are
English; a non-English source only weakens the FTS arm (vector arm unaffected), and per-source
FTS configs are a documented post-MVP change (requires the nuke-and-rebuild path).

### Operational standards (apply to both Python services)

- **Logging:** JSON lines to stdout via `structlog` (fields: `ts`, `level`, `service`, `event`,
  plus context like `source`, `url`, `duration_ms`). `docker compose logs` stays the aggregation
  story; no log shipper in the MVP.
- **Metrics:** `prometheus-client` with a `/metrics` endpoint on each service. Minimum set —
  ingestion: `pages_fetched_total`, `pages_skipped_unchanged_total`, `chunks_indexed_total`,
  `sync_duration_seconds`, `sync_last_success_timestamp`; mcp-server: `search_requests_total`,
  `search_latency_seconds` (histogram → p95). Scraping is optional; the endpoint existing is the
  requirement — silent retrieval-quality decay is the main failure mode of a RAG pipeline.
- **Resources:** compose `deploy.resources.limits` — ingestion **1.5G**, mcp-server **1G**, db
  **512M**. Both Python images set `OMP_NUM_THREADS=1` and `ORT_NUM_THREADS=2` so ONNX doesn't
  oversubscribe home-lab CPUs when both services hold the model.
- **Shutdown:** per-page transactions mean SIGTERM mid-sync loses at most one page of work and
  re-runs are idempotent (hash-diff). A cooperative cancellation flag checked between pages is
  post-MVP.
- **Auth:** `SYNC_TOKEN` is **required** — the ingestion service refuses to start without it,
  and `/sync` requires `Authorization: Bearer $SYNC_TOKEN`. Even LAN-only, an open sync trigger
  is a DoS vector.

### MCP tool contract

```python
@mcp.tool
def search_docs(query: str, source: str | None = None, limit: int = 5) -> str:
    """Search locally indexed framework/library documentation (static reference
    knowledge: API syntax, config options, examples). Use this INSTEAD of guessing
    framework syntax from memory. NOT for project state or decisions — use Mem0
    for those. `source` optionally filters to one doc set (see list_doc_sources)."""

@mcp.tool
def list_doc_sources() -> str:
    """List indexed documentation sets with their last-sync time."""
```

Return format per hit: `### {heading_path}\n[{url}]({url})\n\n{chunk markdown}` — markdown
string, sources cited so agents can quote/deep-link.

Hybrid retrieval SQL (RRF, k=60): rank by `1/(60+dense_rank)` + `1/(60+fts_rank)` over the
top-30 candidates of each arm, return the fused top-`limit`.

---

## 3. Expanded Task Queue

Tasks are sized for one Spoke each, with acceptance criteria inline. `T1`–`T3` fan out in
parallel; the rest serialize on their `depends_on`.

```jsonc
[
  { "id": "T1", "agent": "docker-selfhosted-engineer", "status": "pending",
    "depends_on": [], "worktree": "wt/T1", "attempts": 0,
    "desc": "Infrastructure foundation. Write docker-compose.yml with services: `db` (image pinned to pgvector/pgvector:0.8.2-pg16 — CVE-2026-3172 fix, NOT the floating pg16 tag; named volume pgdata:/var/lib/postgresql/data, healthcheck pg_isready, credentials from .env, deploy.resources.limits.memory 512M), plus service stubs `ingestion` (port 8080 internal) and `mcp-server` (port 8000 internal) with build contexts ./ingestion, ./mcp-server, depends_on db healthy, memory limits 1.5G / 1G respectively, and external Traefik network named by TRAEFIK_NETWORK env (default traefik_proxy). Write db/init/01_schema.sql exactly per the Data Model section of IMPLEMENTATION_PLAN.md (vector extension, doc_sources/doc_pages/doc_chunks, hnsw vector_cosine_ops m=16 ef_construction=64, GIN fts index); init scripts are numbered — schema evolution is documented nuke-and-rebuild. Write .env.example (POSTGRES_USER/PASSWORD/DB, DOCS_MCP_HOSTNAME, TRAEFIK_NETWORK, SYNC_TOKEN) and .gitignore. Write Makefile targets: up, down, sync, test, backup, restore. Acceptance: `docker compose up db` reaches healthy; `\\dx` shows vector 0.8.2; all three tables and both indexes exist; `docker compose config` passes; memory limits present in rendered config." },

  { "id": "T2", "agent": "python-serverless-engineer", "status": "pending",
    "depends_on": [], "worktree": "wt/T2", "attempts": 0,
    "desc": "Ingestion engine (pure logic, no service yet). Create ingestion/ package with pyproject.toml (deps: httpx, beautifulsoup4, trafilatura, fastembed, psycopg[binary], pyyaml, pydantic, fastapi, uvicorn, structlog, prometheus-client, tokenizers). Implement crawler.py (sitemap.xml discovery with BFS fallback bounded by same-host + max_pages; per-source rate_limit_rps default 1.0; custom User-Agent; respects robots.txt), extract.py (trafilatura → markdown, fallback BS4 text, min-length sanity check), chunker.py (split on markdown headings, window 400–600 tokens with 15% overlap counted with the HuggingFace `tokenizers` BGE tokenizer — NOT tiktoken, wrong vocab for a BERT-family model; never split inside fenced code blocks; carry heading_path breadcrumb), embedder.py (FastEmbed TextEmbedding BAAI/bge-small-en-v1.5, MUST use passage_embed for documents, batch of 32), config.py (pydantic model for the sources.yaml schema in IMPLEMENTATION_PLAN.md §2 — name/base_url/sitemap?/include_prefixes?/exclude_prefixes?/max_pages/language?/rate_limit_rps? — fail fast on duplicate names or invalid entries), and a seed sources.yaml with fastapi + nextjs + pgvector-readme. Structured JSON logging via structlog per the Operational standards section. Contract: chunker emits {url, heading_path, chunk_index, content}; embedder adds embedding: list[float] len 384. Acceptance: unit-testable pure functions; invalid sources.yaml (dup name, bad url) raises at load; a local run against one seed source produces >0 chunks each with 384-dim embeddings and non-empty heading paths." },

  { "id": "T3", "agent": "developer", "status": "pending",
    "depends_on": [], "worktree": "wt/T3", "attempts": 0,
    "desc": "Retrieval layer + MCP server skeleton. Create mcp-server/ with pyproject.toml (fastmcp>=3,<4 — FastMCP 3.x API, fastembed, psycopg[binary], psycopg_pool, structlog, prometheus-client). server.py: mcp = FastMCP('self-docs') exposing search_docs(query, source=None, limit=5) and list_doc_sources() with the exact docstrings from the MCP tool contract section of IMPLEMENTATION_PLAN.md (they steer agent routing — copy verbatim); serve with mcp.run(transport='http', host='0.0.0.0', port=8000, stateless_http=True) — note stateless_http moved OFF the constructor in FastMCP 3.x. retrieval.py: embed query via FastEmbed query_embed, hybrid search = top-30 by embedding <=> cosine + top-30 by ts_rank over fts, fused with RRF k=60 in a single SQL CTE, optional source-name filter; format hits as '### heading_path' + markdown link + chunk content. Structured JSON logs + /metrics (search_requests_total, search_latency_seconds histogram) per Operational standards. Contract with T2: reads doc_chunks/doc_pages/doc_sources exactly per T1 schema. Acceptance: server starts against a seeded DB; MCP Inspector (npx @modelcontextprotocol/inspector) lists both tools and search_docs returns formatted markdown with URLs; /metrics responds." },

  { "id": "T4", "agent": "python-serverless-engineer", "status": "pending",
    "depends_on": ["T1", "T2"], "worktree": "wt/T4", "attempts": 0,
    "desc": "Sync orchestration + drift detection + service wrapper. In ingestion/: store.py (SHA-256 page hash; skip unchanged pages; for changed/new pages delete doc_pages row and reinsert page+chunks in one transaction; delete pages absent from the crawl; update doc_sources.last_synced/last_status; per-source outcome ok|partial|failed) and app/main.py FastAPI service on port 8080: POST /sync {sources?: [names]} runs sync as background task guarded by an asyncio lock (409 if already running), GET /status returns last run summary per source incl. pages_fetched/skipped/chunks_indexed and running flag, GET /health, GET /metrics (prometheus-client: pages_fetched_total, pages_skipped_unchanged_total, chunks_indexed_total, sync_duration_seconds, sync_last_success_timestamp). SYNC_TOKEN env is REQUIRED — service exits at startup if unset; /sync requires Authorization: Bearer $SYNC_TOKEN. Dockerfile (python:3.12-slim, non-root, pre-download the FastEmbed model at build time, ENV OMP_NUM_THREADS=1 ORT_NUM_THREADS=2). Acceptance: two consecutive syncs of the same site — second run skips all unchanged pages (log proves it); mutating one page re-embeds only that page; /sync without valid Bearer → 401; missing SYNC_TOKEN → startup failure; /status reflects outcomes; container runs non-root." },

  { "id": "T5", "agent": "docker-selfhosted-engineer", "status": "pending",
    "depends_on": ["T1", "T3"], "worktree": "wt/T5", "attempts": 0,
    "desc": "Dockerize MCP server + Traefik routing. mcp-server/Dockerfile (python:3.12-slim, non-root, model pre-downloaded at build, ENV OMP_NUM_THREADS=1 ORT_NUM_THREADS=2, healthcheck on /mcp reachability). Finish compose: Traefik labels on mcp-server (router Host from DOCS_MCP_HOSTNAME env, entrypoint websecure or internal http per existing home-lab convention, loadbalancer.server.port=8000, flushInterval=-1 / no buffering so SSE streaming works, plus a Traefik rate-limit middleware ~20 req/s burst 50 on the MCP router so a looping agent can't hammer Postgres). ingestion gets NO Traefik exposure — n8n reaches http://ingestion:8080 by container DNS. db gets NO published ports. External network name from TRAEFIK_NETWORK env (T1 contract). Acceptance: `docker compose up -d` brings up all services healthy; `curl https://$DOCS_MCP_HOSTNAME/mcp` answers through Traefik; hammering the endpoint returns 429 past the limit; db and ingestion unreachable from host network." },

  { "id": "T6", "agent": "automation-workflow-engineer", "status": "pending",
    "depends_on": ["T4", "T5"], "worktree": "wt/T6", "attempts": 0,
    "desc": "n8n automation. Build workflow (export JSON to docs/n8n/docs-sync.json): Schedule trigger weekly (Sun 03:00) → HTTP POST http://ingestion:8080/sync with Bearer SYNC_TOKEN from the n8n credential store → Wait/poll GET http://ingestion:8080/status until running=false (poll interval and timeout as workflow variables, default timeout 60 min) → IF any source failed/partial OR trigger errored → generic HTTP-webhook notification node (webhook URL from n8n credential — platform-agnostic, works for Discord/Slack/ntfy) with source names and error text; success path posts a summary (pages changed / chunks written from /status). Handle the 409 already-running case as a no-op, not an alert. Acceptance: workflow JSON imports cleanly into n8n; no secrets embedded in the exported JSON; a manual execution against the live stack completes and the failure branch fires when pointed at a bogus source." },

  { "id": "T7", "agent": "developer", "status": "pending",
    "depends_on": ["T4", "T5"], "worktree": "wt/T7", "attempts": 0,
    "desc": "Agent wiring + memory-boundary rules + runbook. Write AGENTS.md (mirrored/symlinked as CLAUDE.md) with the routing rule: Mem0 = dynamic project state (decisions, preferences, task context); self-docs search_docs = static framework/library syntax and API reference — always search before writing framework-specific code, never store project state in docs nor framework syntax in Mem0. Write docs/client-setup.md with copy-paste configs: Cursor ~/.cursor/mcp.json and project .mcp.json ({\"mcpServers\":{\"self-docs\":{\"type\":\"http\",\"url\":\"https://<host>/mcp\"}}}), Claude Code (`claude mcp add --transport http self-docs https://<host>/mcp`), Antigravity mcp_config.json equivalent. Write docs/runbook.md covering: add a new doc source (edit sources.yaml → POST /sync); re-index from scratch (nuke-and-rebuild: compose down -v db → up → full sync); BACKUP (make backup → pg_dump -Fc to timestamped file) and RESTORE (make restore FILE=… → createdb + pg_restore, then REINDEX the HNSW index — pg_dump stores the index definition but rebuild time is real; alternatively skip restore entirely and re-sync since the corpus is re-crawlable); expected sync durations per source size; troubleshooting (model download, HNSW rebuild, tail JSON logs, check /metrics). Acceptance: following client-setup.md verbatim connects Claude Code and Cursor to the live endpoint and a real `search_docs` round-trip succeeds from each; backup/restore procedure executed once successfully against the live db." },

  { "id": "T8", "agent": "tester", "status": "pending",
    "depends_on": ["T3", "T4", "T5"], "worktree": "wt/T8", "attempts": 0,
    "desc": "Test suite. pytest across both packages: unit tests for chunker (heading split, token bounds with the BGE tokenizer, overlap, breadcrumbs, code-fence integrity), crawler URL filtering incl. include/exclude_prefixes and robots handling (mocked HTTP), sources.yaml validation (dup name, bad url → error), hash-diff upsert logic against the compose db (NOT testcontainers — compose is already the stack; avoids docker-in-docker), and retrieval RRF SQL (seed known chunks, assert exact-token query hits via FTS arm and paraphrase query hits via vector arm, source filter works). One end-to-end test: fixture mini-site (static HTML served locally) → sync → search_docs returns the planted answer with its URL. Acceptance: `make test` green from repo root against the compose stack (no external CI assumed); e2e test passes." },

  { "id": "T9", "agent": "security-hardening-specialist", "status": "pending",
    "depends_on": ["T6", "T7"], "worktree": null, "attempts": 0,
    "desc": "Security review (advisory, read-only). Audit: secrets only via .env/n8n credentials (nothing committed, none in exported workflow JSON), db not exposed beyond docker network, SYNC_TOKEN enforcement (required at startup, 401 without Bearer), MCP endpoint exposure scope — decision: LAN-only via Traefik network segmentation for MVP, no auth on /mcp; adding FastMCP 3.x auth (OAuth/API key) is the documented path if it ever goes internet-facing — verify it is not internet-reachable, Traefik rate-limit middleware present on the MCP router, pgvector image pinned ≥0.8.2 (CVE-2026-3172), containers non-root with minimal images, Traefik TLS posture. Deliver findings list with severity + concrete fixes. Acceptance: written report; no critical findings left unaddressed before 'done'." },

  { "id": "T10", "agent": "critical-reviewer", "status": "pending",
    "depends_on": ["T8", "T9"], "worktree": null, "attempts": 0,
    "desc": "Final quality gate (mode: code-review) + initial seed sync. Verify each task's acceptance criteria against the merged tree; run `make test`; perform the FIRST REAL SYNC of all three seed sources (fastapi, nextjs, pgvector-readme) via POST /sync and verify all reach last_status=ok with >0 chunks each; exercise one live search_docs call per seed source through Traefik from an MCP client and confirm relevant, URL-cited results; confirm drift-detection skip behavior in logs on a second sync. Verdict: APPROVED or CHANGES REQUESTED with itemized gaps." }
]
```

**Parallelism:** T1+T2+T3 run concurrently (disjoint file scopes: compose/db vs `ingestion/` vs
`mcp-server/`). T4 and T5 can also overlap once their deps land. T9 and T8 overlap. T10 gates
merge.

---

## 4. Phase Narrative (expanded descriptions)

### Phase 1 — Infrastructure & Storage (T1)
Postgres is the only stateful service; everything else is rebuildable. HNSW is created **in the
init script** because the corpus starts empty — if you ever bulk-reload millions of chunks,
drop and recreate the index after loading instead. `m=16, ef_construction=64` are the pgvector
defaults and are right for this corpus size; recall can be tuned per-session later with
`SET hnsw.ef_search`.

### Phase 2 — Ingestion Engine (T2, T4)
The critical design points, in order of how much they affect answer quality:
1. **Extraction quality** (trafilatura) — garbage boilerplate in chunks poisons retrieval.
2. **Heading-aware chunking** — a chunk that starts mid-sentence with no context embeds poorly;
   the breadcrumb restores context for both the embedding and the agent reading the hit.
3. **Asymmetric embedding** — `passage_embed` at index time, `query_embed` at search time.
4. **Hash-diff sync** — page-level SHA-256 makes weekly re-syncs cheap (only changed pages
   re-embed) and gives n8n a precise change summary.
Crawler etiquette: 1 req/sec, honor robots.txt, identifiable User-Agent, hard `max_pages` cap
per source. **As of Phase 6** (§8), sources are rows in `doc_sources` (Postgres), managed via the
admin UI or a `propose_doc_source` MCP proposal — not `sources.yaml` — so adding a doc set is a
no-code operation performed there (`sources.yaml` was the pre-Phase-6 mechanism for this and now
survives only as a one-way, opt-in seed).

### Phase 3 — Retrieval Server (T3, T5)
FastMCP 3.x (`fastmcp>=3,<4`), streamable-HTTP transport, stateless (`stateless_http=True`
passed to `run()`, not the constructor — 3.x breaking change). The **tool docstring is the routing
contract** — it is what Cursor/Claude Code read when deciding which tool to call, so it
explicitly says "use instead of guessing syntax" and "NOT for project state (use Mem0)".
Hybrid RRF matters specifically for developer docs: "what does `revalidatePath` do" needs the
FTS arm; "how do I bust the cache for one route" needs the vector arm.

### Phase 4 — Automation (T6)
n8n owns scheduling; the containers stay cron-free. The workflow is exported to the repo so it
is reviewable and restorable. Failure notification includes per-source status so a single
broken site (DOM change, dead sitemap) alerts without blocking the other sources — that is why
`last_status` supports `partial`.

### Phase 5 — Agent Configuration & Memory Split (T7)
The boundary is enforced at three layers: (1) tool docstrings, (2) `AGENTS.md`/`CLAUDE.md`
rules, (3) Mem0's own tool descriptions if editable. Client configs are plain remote-HTTP MCP
entries — no local process per IDE, one shared server for every agent on the LAN.

---

## 5. Edge Cases & Error Handling

- **Doc site DOM/structure change** → trafilatura degrades gracefully; per-source `partial`/
  `failed` status surfaces it via n8n alert instead of silently indexing garbage. A minimum
  extracted-text-length sanity check per page guards against empty extractions.
- **Sync triggered while running** → 409 from the lock; n8n treats as no-op.
- **Page removed upstream** → pages present in DB but absent from crawl are deleted (cascades
  to chunks) so dead links don't get cited.
- **FastEmbed model cache** → pre-baked into both images at build; runtime works offline.
- **Very long code blocks** → chunker never splits inside a fenced code block; oversize blocks
  become their own chunk.
- **DB restart mid-sync** → per-page transactions mean at worst one page is re-processed;
  hash-diff makes re-runs idempotent.
- **Concurrent agent queries during sync** → readers are unaffected (MVCC); delete+insert per
  page is transactional so no half-indexed page is ever visible.
- **Non-English docs** → `to_tsvector('english', …)` weakens the FTS arm only; vector arm still
  works. `language` field exists in `sources.yaml` for future per-source FTS configs (requires
  nuke-and-rebuild).
- **SIGTERM during sync** → per-page transactions + idempotent hash-diff re-runs make an
  ungraceful stop lose at most one page; cooperative cancellation flag is post-MVP.
- **Sync longer than the n8n poll timeout** → timeout is a workflow variable (default 60 min);
  runbook documents expected durations per source size.

---

## 6. Architect Review Resolutions (Rev 2)

Every gap/question from the 2026-07-18 architect review, with its resolution:

| Item | Resolution |
|---|---|
| GAP-1 / Q1 FastMCP version | **Target 3.x**, pin `fastmcp>=3,<4`. Verified: 3.0 is GA; `stateless_http` moved from constructor to `run()`/`http_app()`. T3 updated. |
| GAP-2 Observability | `structlog` JSON logs + `prometheus-client` `/metrics` on both services; metric set defined in §2 Operational standards. T2/T3/T4 updated. |
| GAP-3 Resource limits | Compose memory limits (ingestion 1.5G, mcp 1G, db 512M) + `OMP_NUM_THREADS=1`/`ORT_NUM_THREADS=2`. T1/T4/T5 updated. |
| GAP-4 Graceful shutdown | **Deferred to post-MVP** (cancellation flag). Mitigated now: per-page transactions + idempotent re-runs; n8n timeout made configurable (default 60 min). |
| GAP-5 Ingestion port | Canonical ports declared in §2: mcp-server **8000**, ingestion **8080**. T1/T4/T5/T6 aligned. |
| GAP-6 Backup/restore | Procedure specified in T7 (pg_dump -Fc via `make backup`; restore + HNSW rebuild note; re-sync as the alternative). Automated scheduling deferred to post-MVP. |
| GAP-7 sources.yaml | Full schema in §2 (adds `exclude_prefixes`, `language`, `rate_limit_rps`) + pydantic fail-fast validation. T2 updated. |
| GAP-8 Migrations | **Nuke-and-rebuild documented** as the schema-evolution path (corpus is re-crawlable); numbered init scripts; Alembic only if non-rebuildable state appears. |
| Q2 Model duplication | Separate model downloads per image (accepted ~200MB duplication; no shared-volume coupling). |
| Q3 Endpoint exposure | LAN-only via Traefik segmentation, no auth on `/mcp` for MVP; FastMCP 3.x auth is the documented internet-facing path. T9 verifies. |
| Q4 Tokenizer | **tiktoken replaced** with HuggingFace `tokenizers` using the BGE vocab (BERT-family model; tiktoken counts the wrong tokens). T2 updated. |
| Q5 Seed sources | fastapi, nextjs, pgvector-readme shipped in `sources.yaml`; T10 performs the first real sync of all three. |
| Q6 Notification platform | Generic HTTP-webhook node, URL from n8n credential store — platform-agnostic. |
| Q7 Traefik network | Parameterized: `TRAEFIK_NETWORK` in `.env` (default `traefik_proxy`). |
| Q8 Shared vs dedicated PG | Dedicated instance confirmed (isolation, independent upgrades, clean RDS migration story). |
| Q9 Retrieval eval | Post-MVP backlog: 10–20 query/expected-chunk eval set before tuning RRF k / chunk size. |
| Q10 Test DB | Compose db, not testcontainers. T8 updated. |
| Q11 CI | `Makefile` targets (`make test`) as the local runner; CI pipeline deferred to post-MVP. |
| T7 depends_on | Now `["T4","T5"]` — runbook needs the sync service to validate. |
| T8 depends_on | Now `["T3","T4","T5"]` — explicit, not transitive via T5. |
| Seed-sync task | Folded into T10's acceptance criteria (first real sync of all seed sources + per-source search validation). |
| T6/T9 agent names | **Rejected**: `automation-workflow-engineer` and `security-hardening-specialist` exist in this project's Spoke table; assignments stand. |
| SYNC_TOKEN optional | **Required** — service refuses to start without it; 401 without Bearer. T4/T9 updated. |
| MCP rate limiting | Traefik rate-limit middleware (~20 r/s, burst 50) on the MCP router. T5 updated. |
| pgvector CVE-2026-3172 | Verified real (CVSS 8.1, parallel-HNSW buffer overflow, 0.6.0–0.8.1). Image pinned to `pgvector/pgvector:0.8.2-pg16`. T1/T9 updated. |

**Post-MVP backlog:** retrieval-quality eval set (Q9), CI pipeline (Q11), Alembic migrations if
needed (GAP-8), cooperative sync cancellation via SIGTERM flag (GAP-4), automated backup
scheduling (GAP-6), per-source FTS language configs, `/mcp` auth for internet exposure.

---

## 7. Sources

- FastMCP HTTP deployment (streamable HTTP, stateless mode): https://gofastmcp.com/deployment/http
- FastMCP 3.0 release + 2→3 upgrade guide (`stateless_http` moved to `run()`): https://jlowin.dev/blog/fastmcp-3 · https://gofastmcp.com/getting-started/upgrading/from-fastmcp-2
- pgvector CVE-2026-3172 (fixed in 0.8.2): https://nvd.nist.gov/vuln/detail/CVE-2026-3172 · https://www.postgresql.org/about/news/pgvector-082-released-3245/
- Streamable HTTP production guide: https://mcpcat.io/guides/building-streamablehttp-mcp-server/
- Claude Code MCP config (`claude mcp add --transport http`): https://code.claude.com/docs/en/mcp
- pgvector README (HNSW, operator classes, build guidance): https://github.com/pgvector/pgvector
- pgvector index guidance 2026 (parallel builds, halfvec trade-offs): https://www.dbi-services.com/blog/pgvector-a-guide-for-dba-part-2-indexes-update-march-2026/
- Supabase HNSW notes: https://supabase.com/docs/guides/ai/vector-indexes/hnsw-indexes
- FastEmbed (default model, query/passage prefixes): https://qdrant.tech/articles/fastembed/ · https://github.com/qdrant/fastembed
- BGE query-instruction discussion: https://huggingface.co/BAAI/bge-small-en-v1.5/discussions/4
- Prior art: https://github.com/joungminsung/OpenDocuments · https://github.com/2dogsandanerd/Knowledge-Base-Self-Hosting-Kit

---

## 8. Phase 6 — Sources-in-Postgres, Admin UI, Scheduler, MCP Proposals

Post-MVP phase. Moves crawl-source config out of `ingestion/config/sources.yaml`
and into the `doc_sources` table, adds a loopback-only admin UI, replaces the
n8n weekly-cron workflow with an opt-in in-process scheduler, and lets an MCP
agent propose new sources subject to human approval. Full operator-facing
detail lives in `docs/runbook.md`; this section is the design-level summary
and supersedes §2's "sources.yaml schema" subsection and any narrative text
elsewhere in this document that still describes `sources.yaml` as the live
config path (see the superseded-notice callouts added at each such spot).

| Area | What changed | Why |
|---|---|---|
| **Config storage** | `doc_sources` gains `sitemap`, `include_prefixes`, `exclude_prefixes`, `max_pages`, `language`, `rate_limit_rps`, `schedule_cron`, `enabled`, `status`, `proposed_by`, `created_at` (`db/init/02_sources_config.sql`). `sources.yaml` becomes a one-way seed, imported only when `IMPORT_SOURCES_YAML_ON_BOOT=1`. | A YAML file re-read on every `/sync` request can't hold per-source `schedule_cron`/`enabled`/`status` state cleanly, and gives no place for an MCP-proposed source to land pending review. |
| **Migration** | `db/init/*.sql` only runs against an empty Postgres data directory. On an existing DB, `02_sources_config.sql` must be applied by hand via `scripts/migrate.sh` (idempotent — `ADD COLUMN IF NOT EXISTS` / guarded `DO` block for the `CHECK` constraint). | `db/init` scripts are a first-boot-only mechanism; there is intentionally no auto-migration path for a running Postgres volume (§2 "Schema evolution" / GAP-8's nuke-and-rebuild philosophy still holds — this migration is the one exception because bulk re-crawling isn't needed, only a schema extension). |
| **Admin UI** | Server-rendered (Jinja2 + vendored htmx) CRUD at `/admin` on the `ingestion` service, loopback-only (`127.0.0.1:8080`, no Traefik router) — full source CRUD, manual per-source sync, pending-proposal approve/reject. Auth: `SYNC_TOKEN` exchanged at `/admin/login` for an HMAC-derived session cookie + CSRF token (both deterministic functions of `SYNC_TOKEN` — no per-session store; rotating `SYNC_TOKEN` is the only revocation mechanism for a leaked cookie). | A CRUD surface over crawl config needs a human-operable UI, but must stay off the LAN/Traefik because it can trigger crawls and mutate `doc_sources` directly. |
| **Scheduler** | New `app.scheduler` module: per-source `schedule_cron`, evaluated by a restricted 5-field cron subset (`*`, `*/N`, bare int, comma-list — no ranges, no named values) implemented from scratch in `sources_repo.py` (no `croniter`/`APScheduler` dependency). `SCHEDULER_ENABLED` defaults OFF. Every scheduling decision logs a distinct structlog event (`fired`/`skipped-not-due`/`skipped-locked`/`errored`), with `skipped-not-due` carrying a `reason`. | Replaces n8n's weekly cron trigger with an in-process equivalent that's per-source-configurable and answerable from logs alone; defaulting OFF avoids double-scheduling against a still-active n8n workflow. |
| **Unified sync lock** | `POST /sync`, the admin UI's manual-sync route, and the scheduler now share ONE `threading.Lock` (`app.main._sync_lock`, wired into `admin.py`/`scheduler.py` via injectable seams to avoid a circular import). | Before this, the three entrypoints had three independent, non-cooperating locks — any two could run a sync concurrently against the same source, corrupting `_delete_missing_pages`'s purge accounting. |
| **MCP proposals** | New `propose_doc_source` MCP tool. Writes a `status='pending'` `doc_sources` row; `proposed_by` stores a truncated SHA-256 hash of the caller's bearer token (`sources_repo.derive_proposed_by`), never the token itself. A `pending` source is uncrawlable (`/sync` refuses it with `403`) until a human approves it in the admin UI. | Lets an agent surface "this doc set should be indexed" without giving it write access to what actually gets crawled — approval stays a human-in-the-loop admin-UI action. |
| **`/sync` API** | Accepts `{"source": id\|name}` (single-source, admin-UI-facing) alongside the existing `{"sources": [names]}`. A DB read failure now returns `503` (previously a `sources.yaml` `ConfigError` returned `400`). Unscoped "sync all" now means "sync all `status='active'`" (excludes pending/rejected). A non-active source is refused `403`; an unknown id/name on the single-source path is `404` (list path's unknown-name case stays `400`). | The DB is now the config source of truth, so its own unavailability is a distinct failure mode from a config *validation* error (which no longer exists at request time); the active/pending/rejected lifecycle needs enforcement at the one chokepoint every sync path funnels through. |
| **n8n** | `docs/n8n/` directory purged from repository as part of architecture cleanup. `scheduler.py` (`SCHEDULER_ENABLED=true`) is the sole built-in scheduler. | Eliminates external dependencies, historical clutter, and double-scheduling hazards between external crons and the built-in scheduler. |

**Status:** this migration has already been applied to this deployment's
live database (see `docs/runbook.md`'s migration section for the exact
command, kept for anyone standing up a second instance or rebuilding from a
pre-Phase-6 volume).
