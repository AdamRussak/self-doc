# Agent Rules — self-docs

Binding for every agent (Cursor, Claude Code, Antigravity, or any other MCP
client) working in this repo or any repo that has `self-docs` wired in as an
MCP server. `CLAUDE.md` is a symlink to this file — edit only here.

## Memory boundary: Mem0 vs self-docs

Two knowledge stores. Never mix them.

- **Mem0** = dynamic project state: decisions made, preferences, task/PR
  context, "why we did X here," TODOs, anything that changes as work
  progresses.
- **self-docs `search_docs`** = static framework/library reference: syntax,
  config options, API signatures, examples — indexed once from upstream docs
  and re-synced periodically. It does not know anything about *this* project.

## Rules

1. **Always call `search_docs` before writing framework-specific code** for
   any library covered by an indexed source. Check `list_doc_sources` first
   if unsure whether a source is indexed — do not guess syntax from training
   data when a live doc source exists.
2. **Never store project state in the docs index.** The docs pipeline
   ingests upstream documentation sites via sources configured in the
   `doc_sources` table (Postgres, not `ingestion/config/sources.yaml` — that
   file is only a one-way seed, imported once when
   `IMPORT_SOURCES_YAML_ON_BOOT=1`) + `/sync`. Do not propose writing
   decisions, task notes, or project-specific config into
   `doc_chunks`/`doc_sources` — that belongs in Mem0.
3. **Never store framework/library syntax in Mem0.** If you learn a fact
   about how a library's API works, that fact belongs in the docs index
   (add/re-sync the source) or is transient — it does not belong in Mem0.
4. **Cite your source.** `search_docs` results include `heading_path` and a
   source URL — quote or link them when using a hit to justify code you
   write. Note: some GitHub-README-derived sources (e.g. `pgvector-readme`)
   index with an empty `heading_path`; this is cosmetic, the URL is still
   valid.
5. **If `search_docs` returns nothing relevant**, say so explicitly rather
   than falling back to unverified memory of the library's API. If you have
   the `propose_doc_source` tool, use it to propose adding the missing
   source (name/base_url/max_pages/...) — this only queues a `pending` row
   for a human to review and approve in the admin UI, it never crawls
   anything on its own. Tell the user the proposal is queued for approval,
   not that the docs are "being indexed." See `docs/runbook.md` for the
   full approval workflow.

## Endpoint

Remote MCP server (streamable HTTP), one shared instance for the whole LAN —
see `docs/client-setup.md` for per-client config and `docs/runbook.md` for
operations.
