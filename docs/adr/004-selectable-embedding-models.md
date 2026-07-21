# ADR-004: Registry-Selectable Embedding Models

**Status:** Accepted
**Date:** 2026-07-21
**Decision makers:** Project owner + architect

## Context

[ADR-001](001-custom-pipeline-over-off-the-shelf.md) fixed the embedding model at
`BAAI/bge-small-en-v1.5` (384-dim), hardcoded as a constant in two places that
cannot import each other: `ingestion/app/embedder.py` and
`mcp-server/app/retrieval.py`. Three problems accumulated:

1. **Retrieval quality was capped** by the smallest model in the BGE family.
   `bge-small` was chosen for CPU friendliness, not accuracy.
2. **Changing the model was a multi-file manual edit** — the two constants, the
   `vector(N)` column in `db/init/01_schema.sql`, both Dockerfiles' pre-bake
   step, and the compose memory limits all had to be changed together and kept
   consistent by hand. Nothing enforced agreement.
3. **The documented asymmetric-prefix contract was false.** ADR-001 and the code
   comments asserted that FastEmbed's `passage_embed()`/`query_embed()` apply the
   BGE `passage:`/`query:` prefixes. Inspection of FastEmbed 0.8 shows both
   methods delegate straight to `embed()` for every non-multitask model
   (bge, mxbai, e5) — **no prefix was ever applied**. Queries were being embedded
   with no instruction prefix at all.

Larger models also need more memory than the previous limits allowed: the
mcp-server was capped at 1G, which a 1024-dim model's ONNX weights (~1.3G) would
OOM on first query.

## Decision

Introduce **`config/models.yaml` as the single source of truth** mapping each
supported model to its `dim`, `mem_ingestion`, `mem_mcp`, `query_prompt`, and
`passage_prompt`, with one row marked as the default.

- **`make configure MODEL=<name>`** (`scripts/configure_model.py`) resolves the
  selection into `.env` (`EMBEDDING_MODEL_NAME`, `EMBEDDING_DIM`,
  `EMBEDDING_QUERY_PROMPT`, `EMBEDDING_PASSAGE_PROMPT`, `INGESTION_MEM_LIMIT`,
  `MCP_MEM_LIMIT`) and renders `db/init/01_schema.sql` from a new
  `01_schema.sql.template` so `vector(N)` matches the model.
- **docker-compose derives** memory limits and the image build arg from those
  vars; both services read the model/prompts from env, falling back to `DEFAULT_*`
  constants that equal the registry default row.
- **The default moves to `mixedbread-ai/mxbai-embed-large-v1`** (1024-dim), with
  `intfloat/multilingual-e5-large`, `BAAI/bge-base-en-v1.5`, and the previous
  `BAAI/bge-small-en-v1.5` also supported.
- **Prompts are applied manually** around plain `embed()`, replacing the
  `passage_embed`/`query_embed` calls that were silently no-ops.
- **Parity tests** (`tests/test_model_registry.py`,
  `mcp-server/tests/test_registry_defaults.py`) assert the committed schema equals
  the rendered template, both services' defaults equal the registry default, and
  the duplicated SSRF helper stays byte-identical across services.

## Rationale

**Why a registry instead of just swapping the constant.** The model choice has
four downstream consequences (vector width, memory, prompts, baked image layer)
that must move together. Encoding them in one table and deriving the rest makes
an inconsistent deployment structurally hard rather than merely discouraged —
the same reasoning as ADR-002's single-source schema handling.

**Why mxbai-embed-large as the default.** It is the strongest English model
FastEmbed supports that still runs CPU-only ONNX with no torch dependency,
preserving ADR-001's local-first, GPU-free constraint. The cost is memory
(1G → 2G per service) and ~3× query-embed latency (~100–150 ms), which is noise
next to the LLM round-trip that consumes the results.

**Why keep the small models in the registry.** CI selects
`BAAI/bge-small-en-v1.5` so the test suite does not download 1.2GB on every run —
which also continuously exercises the `make configure` path. Operators on
constrained hardware get the same escape hatch.

**Why manual prompts rather than fixing FastEmbed usage.** The asymmetric API is
a no-op for these models, so there is nothing to fix upstream-side; explicit
prefixes from the registry are unambiguous and make the per-model differences
visible (mxbai/bge instruct on the query only; e5 prefixes both sides).

## Consequences

- **Changing models requires a full re-embed.** Vectors from different models are
  not comparable, and content-hash change detection would otherwise skip
  unchanged pages. `make reindex` truncates `doc_pages`/`doc_chunks` and re-syncs;
  a dimension change additionally requires recreating the schema
  (ADR-002's nuke-and-rebuild path) and rebuilding both images.
- **Existing deployments must reindex on upgrade** — 384-dim vectors are invalid
  against a `vector(1024)` column.
- **Retrieval quality changed** in both directions of risk: queries now carry the
  instruction prefix they never had, and the model is larger. Validate with
  `make eval` before and after rather than assuming improvement.
- **The registry is a new sync point.** A model added there without a matching
  FastEmbed-supported name or wrong `dim` fails at runtime; the parity tests cover
  default consistency but cannot validate a model FastEmbed does not ship.

## Related

- [ADR-001](001-custom-pipeline-over-off-the-shelf.md) — original fixed-model decision, superseded on this point
- [ADR-002](002-nuke-and-rebuild-schema-evolution.md) — the rebuild path a dimension change depends on
- [Runbook → switch the embedding model](../runbook.md#switch-the-embedding-model)
