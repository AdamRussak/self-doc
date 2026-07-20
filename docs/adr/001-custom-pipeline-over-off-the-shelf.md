# ADR-001: Custom Pipeline Over Off-the-Shelf RAG Platforms

**Status:** Accepted  
**Date:** 2026-07-18  
**Decision makers:** Project owner + architect (pre-dispatch review)

## Context

We need a self-hosted "Cursor `@docs`"-style system: crawl static documentation
sites, embed them locally, and serve semantic search to local LLM agents (Cursor,
Claude Code, Antigravity) over the Model Context Protocol (MCP).

Several off-the-shelf RAG platforms were evaluated during the research phase:

- **OpenDocuments** — open-source doc ingestion + search
- **Knowledge-Base-Self-Hosting-Kit** — self-hosted RAG kit
- **Context7** — context-aware doc retrieval
- **OpenRAG** — open-source RAG framework

## Decision

Build a custom pipeline using:

- **PostgreSQL 16 + pgvector 0.8.2** for vector + full-text storage
- **FastEmbed (BAAI/bge-small-en-v1.5)** for CPU-friendly ONNX embeddings
- **FastMCP 3.x** for MCP-over-HTTP serving (streamable HTTP, stateless)
- **Traefik** for reverse proxy / TLS termination / rate limiting
- **n8n** for weekly sync scheduling and failure alerting *(Note: superseded in Phase 6 by an in-process scheduler inside `ingestion` to eliminate external dependencies and double-scheduling hazards)*

## Rationale

None of the off-the-shelf options provide the combination of:

1. **pgvector / RDS portability** — our DB must be a standard Postgres instance
   that can migrate to AWS RDS or any managed Postgres without forking.
2. **Existing Traefik integration** — the home-lab already runs Traefik as the
   reverse proxy; the solution must integrate via standard Docker labels.
3. **Mem0 memory-boundary design** — a hard architectural split between dynamic
   project state (Mem0) and static reference knowledge (this pipeline) enforced
   at the tool-description level. No off-the-shelf RAG tool models this
   boundary.

All evaluated platforms validate the general architecture (crawl → chunk → embed
→ store → serve), confirming our approach is sound — we just need our own
integration layer.

## Consequences

- **Positive:** Full control over chunking strategy, embedding model, retrieval
  algorithm (hybrid RRF), and the MCP tool contract (docstrings that steer
  agent routing).
- **Positive:** No vendor lock-in; pgvector is a standard Postgres extension.
- **Negative:** Maintenance burden — we own the crawler, chunker, embedder,
  and server code (~4,500 lines across 42 files).
- **Negative:** No community updates or pre-built connectors for new doc site
  formats.
