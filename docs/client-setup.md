# Client setup — connecting an agent IDE to self-docs

The `self-docs` MCP server is a single, shared, remote **streamable HTTP**
endpoint behind Traefik at:

```
https://<DOCS_MCP_HOSTNAME>/mcp
```

Replace `<DOCS_MCP_HOSTNAME>` with the value of `DOCS_MCP_HOSTNAME` from this
repo's `.env` (e.g. `docs-mcp.lan.example.com`). There is no local process to
run per IDE — every client on the LAN points at the same server. The
endpoint is rate-limited to ~20 req/s (burst 50) by Traefik; a single agent
doing normal `search_docs` calls will never hit that.

Two tools are exposed: `search_docs(query, source?, limit?)` and
`list_doc_sources()`. See `AGENTS.md` for the routing rules every client
should follow.

---

## Cursor

### Global (all projects)

Edit `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "self-docs": {
      "type": "http",
      "url": "https://<DOCS_MCP_HOSTNAME>/mcp"
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
      "url": "https://<DOCS_MCP_HOSTNAME>/mcp"
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
claude mcp add --transport http self-docs https://<DOCS_MCP_HOSTNAME>/mcp
```

### Project scope

To scope the server to one project instead of globally, add it with
`--scope project`, which writes a `.mcp.json` in the project root (same file
Cursor reads, same schema — the two clients can share it):

```bash
claude mcp add --transport http self-docs https://<DOCS_MCP_HOSTNAME>/mcp --scope project
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

> Antigravity's MCP config schema moves between releases — verify the exact
> field names against your installed version's docs before relying on this.
> As of writing, Antigravity uses a `serverUrl`-style remote-HTTP entry,
> conceptually equivalent to Cursor's `mcp.json`:

Edit Antigravity's `mcp_config.json`:

```json
{
  "mcpServers": {
    "self-docs": {
      "serverUrl": "https://<DOCS_MCP_HOSTNAME>/mcp"
    }
  }
}
```

If your Antigravity version instead expects `type: "http"` + `url` (the
Cursor-style schema) or a `transport` block, use that form instead — the
important part is a remote HTTP/streamable-HTTP entry pointing at the
`/mcp` path above, with no local command/process.

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
