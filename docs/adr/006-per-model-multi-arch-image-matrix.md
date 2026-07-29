# ADR-006: Per-Model, Multi-Arch Image Matrix

**Status:** Accepted
**Date:** 2026-07-28
**Decision makers:** Project owner + architect

## Context

[ADR-004](004-selectable-embedding-models.md) made the embedding model
registry-selectable (`config/models.yaml`), but the model is still **baked
into each container image at build time** (`ARG EMBEDDING_MODEL_NAME`, both
Dockerfiles) — FastEmbed downloads and caches the ONNX weights during
`docker build`, not at container start. One image therefore cannot serve more
than one model; the release pipeline needed to decide how to publish four
(and growing) model variants per service without turning that into a
hand-maintained list.

Three problems converged on this decision:

1. **`:latest` was previously baked with a 1024-dim model**
   (`mixedbread-ai/mxbai-embed-large-v1`, per ADR-004's original default)
   while the *committed* schema (`db/init/01_schema.sql`, `vector(384)`) and
   both services' code-level fallbacks had already reverted to
   `BAAI/bge-small-en-v1.5`/384-dim (see ADR-004's status update). Anyone
   pulling `:latest` onto a fresh volume got a dimension mismatch on the
   first insert — the exact bug documented and fixed in
   `docs/runbook.md`/`README.md` alongside this change. A tagging scheme
   that ties `:latest` to *whichever* model is the registry default, computed
   from the same source of truth the schema/services read, closes this
   permanently rather than requiring a human to remember to keep them in
   sync.
2. **The `arm64` leg ran under QEMU emulation and was the slowest, flakiest
   part of a release.** `pip install` (no manylinux aarch64 wheel for some
   dependencies, falling back to a source compile under emulation) and the
   FastEmbed ONNX model pre-download/first-run session init both got
   dramatically slower under emulation, occasionally timing out outright.
3. **A single shared `:buildcache` tag cannot serve multiple model
   variants.** Two legs building different models for the same service and
   racing to push to one shared cache tag would clobber each other's layers,
   since a registry cache ref is a single mutable tag, not an additive
   content-addressed store.

## Decision

- **Encode the model in the image *tag*, not the image name.** Two GHCR
  packages continue to exist (`self-docs-ingestion`, `self-docs-mcp-server`);
  every model gets its own slug-suffixed row of tags
  (`<slug>`, `vX.Y.Z-<slug>`, `X.Y.Z-<slug>`, `sha-<sha>-<slug>`) inside the
  same package, and only the registry-default model additionally claims the
  unsuffixed `latest`/`vX.Y.Z`/`X.Y.Z`/`sha-<sha>` tags.
- **Generate the build/merge matrix from `config/models.yaml`**
  (`scripts/models_matrix.py`, consumed by the `prepare` job in
  `.github/workflows/release.yml`), so the workflow itself never hardcodes a
  model name or slug. `tests/test_models_matrix.py` locks this down,
  including a guard that `release.yml` contains no hardcoded model literal.
  Adding a row to the registry (with a unique `image_slug:`) is enough to get
  that model its own images on the next release — no workflow edit required.
- **Native `ubuntu-24.04-arm` runners instead of QEMU** for the arm64 leg of
  every `(service, model)` pair, alongside `ubuntu-24.04` for amd64 — two
  single-arch, native-architecture build legs per `(service, model)` instead
  of one emulated multi-platform build.
- **Push-by-digest, then `docker buildx imagetools create` to merge.** Each
  `build` leg pushes its single-arch image untagged, addressed only by
  digest; a separate `merge` job downloads the two digests for a given
  `(service, model)` and fuses them into one tagged multi-arch manifest list.
  `imagetools create` is used instead of the older `docker manifest create`
  because it understands and re-attaches each per-arch image's
  provenance/SBOM attestation manifests when assembling the merged index;
  `docker manifest` only merges plain image manifests and would silently drop
  that attestation data.
- **One registry cache ref per service+model+arch**
  (`buildcache-<slug>-<arch>`), not one shared `:buildcache` tag, so
  concurrent legs building different models never clobber each other's
  cached layers.

## Consequences

- **16 build legs + 8 merge legs per release** (2 services × 4 models × 2
  platforms, then 2 services × 4 models). This scales linearly with the
  registry: a fifth model adds 4 build legs and 2 merge legs automatically,
  with no workflow change.
- **16 `buildcache-*` package versions** appear in GHCR (one per
  service+model+arch) alongside the release tags — these are build cache,
  not release artifacts, and are called out as such in `docs/runbook.md` so
  nobody mistakes them for something to `docker pull` and run.
- **`multilingual-e5-large` variants are the heaviest** — its ONNX weights
  are ~2 GB, the largest of the four registry rows, so those four build legs
  (2 services × 2 platforms) take the longest and produce the largest pushed
  layers.
- **Layers dedupe across variants.** The base image, Python/OS dependencies,
  and application code layers are identical regardless of which model is
  baked in — only the final ONNX-weights layer differs — so GHCR storage
  growth across 8 image variants (2 services × 4 models) is far less than a
  naive 8× multiple of a single variant's size.
- **Native arm64 runners are free on public GitHub repos and hard-fail the
  job with no automatic fallback on private ones.** This repo is public (per
  `CLAUDE.md`/the global project context) specifically so this holds. If the
  repo is ever made private, the arm64 build leg must be repointed at a paid
  larger arm64 runner, or the whole platform axis reverted to a single
  amd64+QEMU leg — this is a real constraint on any future decision to go
  private, not a hypothetical.
- **Adding a model to the registry now automatically produces images.** A new
  `config/models.yaml` row (with `dim`, `mem_ingestion`, `mem_mcp`, prompts,
  and a unique `image_slug:`) is picked up by `scripts/models_matrix.py` on
  the very next release run; nothing in `.github/workflows/release.yml`
  needs to change.
- **Verified on release `v0.1.0` (commit `169ac2e`):** `latest` and
  `bge-small-en-v1.5` resolve to the identical digest
  (`sha256:b8bfc48a8a47…`), confirming the "only the registry-default model
  gets the unsuffixed `latest`" rule holds in practice and that the
  1024-dim-`latest`-against-384-dim-schema bug from problem 1 above is
  closed in the registry, not merely in source. All 8 variants (2 services ×
  4 models) were confirmed multi-arch (`linux/amd64` + `linux/arm64`) and
  carrying correct `io.self-docs.embedding-model` / `io.self-docs.embedding-dim`
  labels. See "Open risk — resolved" below for the attestation-survival
  verification from the same release.

## Alternatives considered

1. **Add the model axis to the existing single QEMU job.** Simplest possible
   change — extend the one existing build job's matrix with a `model`
   dimension and keep amd64+QEMU for arm64. Rejected: this multiplies the
   already-slowest, flakiest step in the pipeline by the number of models
   instead of fixing it, turning an occasional timeout into a per-release
   near-certainty across four models.
2. **Docker's `docker/github-builder` reusable workflow with
   `distribute: true`.** This first-party reusable workflow already
   implements the per-platform build / push-by-digest / finalize-into-a-
   manifest-list pattern this ADR hand-rolls. Rejected for now to keep
   control over per-`(service, model, arch)` cache refs and the conditional
   `latest` tag (only the registry-default model gets it) — both of which
   would need to be threaded through or worked around if delegated to an
   external reusable workflow. Noted as the fallback if the hand-rolled
   `build`/`merge` split in `release.yml` proves too fragile to maintain.
3. **One GHCR package per model**
   (`self-docs-ingestion-bge-small-en-v1.5`, `self-docs-ingestion-mxbai-embed-large-v1`,
   …). Rejected: 8 packages (2 services × 4 models) to browse, secure, and
   retain instead of 2, and `latest` loses its meaning entirely — there would
   be no single package where "the recommended default" and "every supported
   variant" coexist.

## Open risk — resolved

`provenance: mode=max` + `sbom: true` (set on every `build` leg) surviving
the push-by-digest → `docker buildx imagetools create` merge was documented
upstream behavior, but at the time this ADR was first written had not been
observed on a real run of this repo's `release.yml`. **This is now resolved:
release `v0.1.0`, built from commit `169ac2e`, published all eight variants
(2 services × 4 models), and the merged index was inspected directly:**

```
$ docker buildx imagetools inspect ghcr.io/adamrussak/self-docs-ingestion:bge-small-en-v1.5
Name:      ghcr.io/adamrussak/self-docs-ingestion:bge-small-en-v1.5
MediaType: application/vnd.oci.image.index.v1+json
Digest:    sha256:b8bfc48a8a47dcfa7f1d9da162555eaa9a26fa05a1c76772f440c70c017d0cf6

Manifests:
  ...@sha256:9e63fa25...  Platform: linux/arm64
  ...@sha256:9f548f1b...  Platform: unknown/unknown
    Annotations:
      vnd.docker.reference.digest: sha256:9e63fa25...
      vnd.docker.reference.type:   attestation-manifest
  ...@sha256:e4469b47...  Platform: linux/amd64
  ...@sha256:7d340d49...  Platform: unknown/unknown
    Annotations:
      vnd.docker.reference.digest: sha256:e4469b47...
      vnd.docker.reference.type:   attestation-manifest
```

Two platform manifests (amd64, arm64) plus one attestation manifest per
platform, each annotated with the digest of the platform manifest it attests
to — the provenance/SBOM attestations survived the push-by-digest → merge
intact, exactly as `imagetools create` promises and `docker manifest create`
does not.

The fallbacks below were the contingency plan if attestations had *not*
survived the merge. They are **retained for context, not as live
guidance** — they were not needed:

> Dropping to plain `provenance: true` (no SBOM, less metadata but simpler
> to reason about across the merge), or a separate post-merge
> attestation-attach step.

## Related

- [ADR-004](004-selectable-embedding-models.md) — introduced the model
  registry this ADR's build matrix is generated from
- [Runbook → Pre-built Container Images (GHCR)](../runbook.md#pre-built-container-images-ghcr)
- `.github/workflows/release.yml`, `scripts/models_matrix.py`,
  `tests/test_models_matrix.py`
