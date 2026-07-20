# self-docs

<p align="center">
  <img src="docs/assets/hero_banner.png" alt="self-docs — Self-Hosted Documentation RAG & MCP Pipeline" width="100%" />
</p>

> A self-hosted documentation RAG pipeline for LLM agents — crawl static docs
> sites, embed them locally with pgvector, and serve semantic search over the
> Model Context Protocol.

<p>
  <img alt="PostgreSQL 16" src="https://img.shields.io/badge/PostgreSQL-16-336791">
  <img alt="pgvector 0.8.2" src="https://img.shields.io/badge/pgvector-0.8.2-4169E1">
  <img alt="FastMCP 3.x" src="https://img.shields.io/badge/FastMCP-3.x-6E56CF">
  <img alt="Protocol: MCP" src="https://img.shields.io/badge/protocol-MCP-000000">
  <img alt="License: Private" src="https://img.shields.io/badge/license-Private-lightgrey">
</p>

**self-docs** gives your coding agents (Cursor, Claude Code, Antigravity, or any
MCP client) a private, always-current reference library. It crawls upstream
documentation sites, chunks and embeds them locally — no third-party embedding
API — and exposes hybrid semantic search as MCP tools over streamable HTTP.

Static reference knowledge lives here. Dynamic project state stays in
[Mem0](https://mem0.ai). The two never mix — see [`CLAUDE.md`](CLAUDE.md) for
the memory boundary that governs agents in this repo.

---

## Contents

- [Why self-docs](#why-self-docs)
- [Architecture](#architecture)
- [Quickstart — Local Development](#quickstart--local-development)
- [Quickstart — Production (Home-Lab + Traefik)](#quickstart--production-home-lab--traefik)
- [MCP Tools](#mcp-tools)
- [Managing Sources](#managing-sources)
- [Documentation](#documentation)
- [Development](#development)
- [License](#license)

---

## Why self-docs

- **Local-first embeddings.** FastEmbed (`BAAI/bge-small-en-v1.5`) runs
  in-process; documentation never leaves your network.
- **Hybrid retrieval.** Vector similarity + per-source-language Postgres
  full-text search over `pgvector`, so exact terms and semantic matches both
  surface.
- **Efficient re-crawling.** Sources can prefer a site's
  [llms.txt](https://llmstxt.org) index over HTML crawling, and re-syncs use
  HTTP conditional GET (`ETag`/`If-Modified-Since`) to skip unchanged pages
  before download — see [ADR-003](docs/adr/003-llms-txt-etag-multilang-fts.md).
- **Agent-native.** Ships as MCP tools (`search_docs`, `list_doc_sources`,
  `propose_doc_source`) over streamable HTTP — wire it into any MCP client.
- **Operator-friendly.** Crawl targets live in the database, managed through a
  loopback-only admin UI or proposed by agents for human approval.
- **Self-hostable.** One `docker compose` stack; a Traefik overlay for
  home-lab ingress.

## Architecture

```
  Cursor ──┐            ┌─────────┐   ┌──────────────┐
  Claude ──┼─ HTTP ──▶  │ Traefik │──▶│ FastMCP srv  │──┐
  Antigrav ┘  /mcp      └─────────┘   │ (search_docs,│  │ SQL
                               │      │  propose_    │  │
                               │      │  doc_source) │  ▼
  operator ── loopback ───────▶│      └──────────────┘ ┌────────┐
  (/admin UI, 127.0.0.1:8080)  └─────▶│ Ingestion    │▶│ pg16 + │
  internal scheduler ────────────────▶│ svc (FastAPI)│ │pgvector│
  (opt-in, per-source cron)           └──────────────┘ └────────┘
```

| Layer | Technology |
|-------|------------|
| Store | PostgreSQL 16 + pgvector 0.8.2 |
| Embeddings | FastEmbed · `BAAI/bge-small-en-v1.5` |
| MCP server | FastMCP 3.x (streamable HTTP) |
| Ingestion | FastAPI crawler + chunker + scheduler |
| Ingress | Traefik (production overlay) |

Source configuration (crawl targets, URL prefixes, schedule) lives in the
`doc_sources` table — **not** a YAML file. Sources are managed through the
loopback-only admin UI at `/admin`, or proposed by an agent via the
`propose_doc_source` MCP tool (which queues a `pending` row for human approval
and never crawls on its own). The ingestion service includes an opt-in in-process
cron scheduler (`app.scheduler`) for automated re-crawling; see the
[Runbook](docs/runbook.md) for configuration details.

## Quickstart — Local Development

```bash
cp .env.example .env        # fill in real values
make up                     # db + ingestion (:8080) + mcp-server (:8081)
make sync                   # trigger the initial documentation sync
```

Point local MCP clients at `http://127.0.0.1:8081/mcp` (streamable HTTP). The
server requires an `Authorization: Bearer <MCP_TOKEN>` header — see
[Client Setup](docs/client-setup.md) for per-client configuration.

## Quickstart — Production (Home-Lab + Traefik)

Deploy behind Traefik ingress on a home-lab server:

```bash
cp .env.example .env                    # set credentials + DOCS_MCP_HOSTNAME
export MCP_TOKEN=$(openssl rand -hex 32)  # required — persist this in .env
make up-prod                            # applies docker-compose.prod.yml overlay
make sync                               # trigger the initial documentation sync
```

> [!IMPORTANT]
> **`MCP_TOKEN` is mandatory.** If it is missing from `.env`, `mcp-server`
> fails fast on startup and restart-loops. When upgrading an existing
> deployment, update every client config with the `Authorization` header
> **before or alongside** restarting `mcp-server`. Follow the
> [MCP_TOKEN upgrade checklist](docs/runbook.md#deploy--upgrade--mcp_token-requirement-read-before-restarting-mcp-server)
> in the runbook.

## MCP Tools

| Tool | Description |
|------|-------------|
| `search_docs(query, source?, limit?)` | Hybrid vector + full-text search over indexed docs |
| `list_doc_sources()` | List indexed documentation sets with sync status |
| `propose_doc_source(name, base_url, max_pages, ...)` | Propose a new source; lands as `pending` and stays uncrawlable until approved in the admin UI — never crawls itself |

## Managing Sources

| Action | How |
|--------|-----|
| Add / edit / remove a source | Admin UI at `http://127.0.0.1:8080/admin` (loopback only) |
| Agent-proposed source | `propose_doc_source` MCP tool → `pending` → human approval |
| Trigger a sync | `make sync` (or the per-source internal scheduler) |
| Approval workflow | [Runbook → adding sources](docs/runbook.md) |

## Documentation

| Guide | What's inside |
|-------|---------------|
| **[Client Setup](docs/client-setup.md)** | Connect Cursor, Claude Code, and Antigravity |
| **[Runbook](docs/runbook.md)** | DB migration, adding sources, the internal scheduler, backup/restore, troubleshooting |
| **[Architecture Decisions](docs/adr/)** | ADRs documenting key design choices |

## Development

```bash
# Start an isolated db for testing
docker compose -f docker-compose.yml -f docker-compose.test.yml up -d db

# Run the full suite (unit + integration + e2e)
make test

# Run the retrieval-quality eval (requires a synced db)
make eval
```

Backup and restore are available via `make backup`, `make backup-prune`, and
`make restore FILE=backups/docs_<timestamp>.dump` — see the
[Runbook](docs/runbook.md) for the full procedure.

## License

Private — not published.
