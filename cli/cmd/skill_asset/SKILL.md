---
name: doc-cli
description: >
  Progressive disclosure terminal workflow using the Go doc-cli tool to query self-docs over the HTTP API. Use doc-cli search to fetch minimal candidate IDs and snippets, then doc-cli get <id> to retrieve exact chunk markdown.
---

# doc-cli — Progressive Disclosure Documentation Workflow

Use `doc-cli` to query `self-docs` via high-performance, token-optimized HTTP API calls.

## Protocol for AI Agents

1. **Search First (Token-Efficient)**:
   ```bash
   doc-cli search "<query>" --limit 3
   ```
   *Outputs candidate IDs, heading paths, scores, and 1-line snippets (~50–150 tokens).*

2. **Filtered Search (Optional)**:
   ```bash
   doc-cli search "<query>" --source fastapi --limit 3
   ```

3. **Inspect Results**:
   Evaluate candidate IDs from standard output.

4. **Targeted Fetch by ID**:
   ```bash
   doc-cli get <id>
   ```
   *Fetches exact markdown content for the target chunk ID.*

5. **Hierarchy Overview**:
   ```bash
   doc-cli tree
   ```
   *Lists all indexed documentation sources, page counts, chunk counts, and sync timestamps.*

## Global Options

- `--url <url>`: Ingestion REST API endpoint (default `http://localhost:8080` or `$SELF_DOCS_API_URL`).
- `--token <token>`: Bearer authentication token (`$API_TOKEN` / `$SYNC_TOKEN` / `$MCP_TOKEN`).
- `--json`: Format output as machine-readable JSON.
- `--compact`: Format output as compact single-line text.
- `--limit <n>`: Clamped limit on candidate search results (default 3).
- `--verbose`: Enable detailed error trace output.
