# self-docs

A self-hosted MCP documentation RAG pipeline: crawl static documentation sites,
embed them locally with pgvector, and serve semantic search to LLM agents
(Cursor, Claude Code, Antigravity) over the Model Context Protocol.

Static reference knowledge lives here; dynamic project state stays in Mem0.

## Architecture

```
  Cursor ──┐            ┌─────────┐   ┌──────────────┐
  Claude ──┼─ HTTP ──▶  │ Traefik │──▶│ FastMCP srv  │──┐
  Antigrav ┘  /mcp      └─────────┘   │ (search_docs)│  │ SQL
                               │      └──────────────┘  ▼
  n8n cron ── webhook ────────▶│      ┌──────────────┐ ┌────────┐
  (weekly + alerts)            └─────▶│ Ingestion    │▶│ pg16 + │
                                      │ svc (FastAPI)│ │pgvector│
                                      └──────────────┘ └────────┘
```

**Stack:** PostgreSQL 16 + pgvector 0.8.2 · FastEmbed (BAAI/bge-small-en-v1.5) ·
FastMCP 3.x (streamable HTTP) · Traefik · n8n

## Quickstart (Local Development)

```bash
cp .env.example .env        # fill in real values
make up                     # starts db, ingestion (port 8080), and mcp-server (port 8081 locally)
make sync                   # trigger initial documentation sync
```

Local clients connect to `http://127.0.0.1:8081/mcp` (streamable HTTP; requires an
`Authorization: Bearer <MCP_TOKEN>` header — see `docs/client-setup.md`).

## Quickstart (Production / Home-Lab with Traefik)

To deploy with Traefik ingress routing on a home-lab server:

```bash
cp .env.example .env        # fill in credentials and DOCS_MCP_HOSTNAME
                             # also set MCP_TOKEN=$(openssl rand -hex 32) — required, see below
make up-prod                # applies docker-compose.prod.yml overlay for Traefik ingress
make sync                   # trigger initial documentation sync
```

**`MCP_TOKEN` is required.** If it's missing from `.env`, `mcp-server` fails
fast on startup and restart-loops. If you're upgrading an existing deployment,
update all client configs with the `Authorization` header *before or
alongside* restarting `mcp-server` — see [Deploy / Upgrade — MCP_TOKEN
requirement](docs/runbook.md#deploy--upgrade--mcp_token-requirement-read-before-restarting-mcp-server)
in the runbook for the full checklist.

## MCP Tools

| Tool | Description |
|------|-------------|
| `search_docs(query, source?, limit?)` | Hybrid vector + full-text search over indexed docs |
| `list_doc_sources()` | List indexed documentation sets with sync status |

## Documentation

- **[Client Setup](docs/client-setup.md)** — connect Cursor, Claude Code, Antigravity
- **[Runbook](docs/runbook.md)** — add sources, backup/restore, troubleshooting
- **[Architecture Decisions](docs/adr/)** — ADRs documenting key design choices
- **[n8n Workflow](docs/n8n/README.md)** — automated weekly sync + alerting

## Development

```bash
# Start db for testing
docker compose -f docker-compose.yml -f docker-compose.test.yml up -d db

# Run all tests (56 unit + integration + e2e)
make test

# Run retrieval quality eval (requires synced db)
make eval
```

## License

Private — not published.
