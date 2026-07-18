# Client setup — connecting an agent IDE to self-docs

# Client setup — connecting an agent IDE to self-docs

When running **locally in development** (`make up`), the server exposes a loopback streamable HTTP endpoint at:

```
http://127.0.0.1:8081/mcp
```
*(or port `8000` if `${DOCS_MCP_HOST_PORT}` is set to 8000).*

When deployed **in production / home-lab behind Traefik** (`make up-prod`), the server is exposed at:

```
https://<DOCS_MCP_HOSTNAME>/mcp
```

Replace `<DOCS_MCP_HOSTNAME>` with the value of `DOCS_MCP_HOSTNAME` from `.env`. When behind Traefik, the endpoint is rate-limited to ~20 req/s (burst 50).

Every request — local or remote — must include an `Authorization: Bearer <MCP_TOKEN>`
header, where `<MCP_TOKEN>` is the value of the `MCP_TOKEN` environment variable from
`.env`. Requests without a valid token receive `401 Unauthorized`.

Two tools are exposed: `search_docs(query, source?, limit?)` and `list_doc_sources()`. See `AGENTS.md` for the routing rules every client should follow.

---

## Copy-paste `mcp.json` samples

**Local dev:**

```json
{
  "mcpServers": {
    "self-docs": {
      "type": "http",
      "url": "http://127.0.0.1:8081/mcp",
      "headers": {
        "Authorization": "Bearer <MCP_TOKEN>"
      }
    }
  }
}
```

**Remote (production / home-lab via Traefik):**

```json
{
  "mcpServers": {
    "self-docs": {
      "type": "http",
      "url": "https://<DOCS_MCP_HOSTNAME>/mcp",
      "headers": {
        "Authorization": "Bearer <MCP_TOKEN>"
      }
    }
  }
}
```

---

## Cursor

### Global (all projects)

Edit `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "self-docs": {
      "type": "http",
      "url": "https://<DOCS_MCP_HOSTNAME>/mcp",
      "headers": {
        "Authorization": "Bearer <MCP_TOKEN>"
      }
    }
  }
}
```

### Project-scoped (this repo / any repo that wants it)

Create `.mcp.json` at the project root (same schema, scoped to that project
only):

```json
{
  "mcpServers": {
    "self-docs": {
      "type": "http",
      "url": "https://<DOCS_MCP_HOSTNAME>/mcp",
      "headers": {
        "Authorization": "Bearer <MCP_TOKEN>"
      }
    }
  }
}
```

Restart Cursor (or reload the MCP servers panel) after editing either file.

**Verify:** open Cursor's MCP settings/tools panel and confirm `self-docs` is
listed as connected with tools `search_docs` and `list_doc_sources`. Then, in
chat, ask the agent to call `list_doc_sources` — you should get back the
seed sources (`fastapi`, `nextjs`, `pgvector-readme`) with their last-sync
times.

---

## Claude Code (CLI)

Add the server once, globally:

```bash
claude mcp add --transport http self-docs https://<DOCS_MCP_HOSTNAME>/mcp \
  --header "Authorization: Bearer <MCP_TOKEN>"
```

### Project scope

To scope the server to one project instead of globally, add it with
`--scope project`, which writes a `.mcp.json` in the project root (same file
Cursor reads, same schema — the two clients can share it):

```bash
claude mcp add --transport http self-docs https://<DOCS_MCP_HOSTNAME>/mcp \
  --header "Authorization: Bearer <MCP_TOKEN>" --scope project
```

**Verify:**

```bash
claude mcp list
```

should show `self-docs` with a connected/healthy status. Then, in a Claude
Code session, ask it to call `list_doc_sources` (or just `search_docs` with
a query like "fastapi dependency injection") and confirm it returns
markdown hits with `heading_path` and a source URL.

---

## Antigravity

> Antigravity supports both `type: "http"` + `url` (Cursor-style schema) and
> `serverUrl`-style remote entries depending on version/release. Both work with
> `self-docs` streamable HTTP endpoint over `/mcp`.

Edit Antigravity's `mcp_config.json` (`~/.gemini/config/mcp_config.json`):

```json
{
  "mcpServers": {
    "self-docs": {
      "type": "http",
      "url": "https://<DOCS_MCP_HOSTNAME>/mcp",
      "headers": {
        "Authorization": "Bearer <MCP_TOKEN>"
      }
    }
  }
}
```
*(For local development against `make up`, use `"url": "http://127.0.0.1:8081/mcp"` and replace `<MCP_TOKEN>` with your literal `MCP_TOKEN` secret value from `.env`.)*

If your Antigravity release expects `serverUrl` instead (`"serverUrl": "https://<DOCS_MCP_HOSTNAME>/mcp"`), use that field name — the important part is pointing at the `/mcp` path with the `Authorization: Bearer <MCP_TOKEN>` header set and no local command/process.

**Verify:** open Antigravity's MCP/tools panel, confirm `self-docs` shows as
connected with `search_docs` and `list_doc_sources` available, then run a
`list_doc_sources` call from a chat/agent session and confirm the seed
sources come back.

---

## Troubleshooting a failed connection

- **TLS/hostname errors** — confirm `DOCS_MCP_HOSTNAME` resolves on your LAN
  and Traefik has a valid cert for it (or that your client trusts the
  home-lab CA).
- **404 / connection refused** — confirm `docker compose up -d` has
  `mcp-server` healthy (`docker compose ps`) and Traefik's router picked up
  the `self-docs-mcp` service (check the Traefik dashboard).
- **429 Too Many Requests** — you're past the 20 r/s / burst-50 rate limit;
  back off retry frequency.
- **Tools list is empty / tools don't show up** — some clients cache MCP
  tool lists; fully restart the client rather than just reloading the
  config.
- See `docs/runbook.md` for server-side troubleshooting (logs, /metrics,
  auth, empty `heading_path`).
