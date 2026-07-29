# `deploy/` — standalone image-based deployment kit

This directory is the whole answer to "run self-docs from pre-built GHCR
images, no clone, no build toolchain." It is self-contained: `install.sh`
copies the other three files into a fresh install directory and never reads
anything else from this repository. See
[README → Quickstart — Pre-Built Images](../README.md#quickstart--pre-built-images-no-clone)
for the end-to-end walkthrough; this file is the reference for what's in the
kit and exactly what the installer does.

## Files

| File | What it is |
|------|------------|
| `install.sh` | The installer. Resolves the model, renders `db/init/01_schema.sql` for its dimension, writes a `0600` `.env` with generated secrets, pulls the two GHCR images, verifies their `io.self-docs.*` labels against the selected model **before** starting anything, then brings the stack up and waits for health. See "Flag reference" and "Exit codes" below. |
| `docker-compose.yml` | The compose file the installer copies into the install directory as `docker-compose.yml`. Image-only (no `build:` — there is no source tree in an install directory), both ports bound to `127.0.0.1`, no fixed container/network names (so more than one install — e.g. one per model — can coexist on the same host). Excludes `renderer` (no published image; source-build only) and any reverse-proxy/Traefik wiring (host-specific, layered on separately). |
| `.env.example` | Documents all 16 variables the compose file consumes (`SELF_DOCS_OWNER`, `SELF_DOCS_IMAGE_TAG`, `POSTGRES_USER`/`PASSWORD`/`DB`, `SYNC_TOKEN`, `MCP_TOKEN`, `EMBEDDING_MODEL_NAME`, `EMBEDDING_DIM`, `EMBEDDING_QUERY_PROMPT`, `EMBEDDING_PASSAGE_PROMPT`, `INGESTION_MEM_LIMIT`, `MCP_MEM_LIMIT`, `SELF_DOCS_API_PORT`, `SELF_DOCS_MCP_PORT`, `TZ`). Only useful for the manual install path (the collapsed "Manual install (no script)" block in the [README](../README.md#quickstart--pre-built-images-no-clone)) — `install.sh` writes its own `.env` from generated values and does not read this file. |
| `models.tsv` | **Generated** — `make deploy-manifest` runs `python3 scripts/models_matrix.py --format tsv` and overwrites this file from `config/models.yaml`, the single source of truth for the model registry. **Do not hand-edit `models.tsv`.** If you change `config/models.yaml`, re-run `make deploy-manifest` and commit the diff; a committed copy that drifts from what that command produces is a bug, not a customization point. |

`install.sh` resolves `models.tsv` with a three-way lookup, in priority order:
`--source-dir <path>/deploy/models.tsv` → `models.tsv` next to `install.sh` on
disk → fetched over the network from the script's `BASE_URL`. This lets the
documented "download just `install.sh` and run it" flow work with nothing
else on disk (case 3), while still supporting an offline/airgapped install
from a full checkout (case 1) or a whole downloaded `deploy/` directory
(case 2).

## What this installer trusts

This is an honest accounting of the trust model, not a warning to talk you
out of using it.

- **HTTPS protects the wire, not the source.** Every network fetch in
  `install.sh` (`db/init/*.sql`, `deploy/docker-compose.yml`,
  `deploy/models.tsv` when resolved from the network) goes over
  `https://raw.githubusercontent.com/...`, which stops eavesdropping and
  MITM. It does **nothing** against a compromised GitHub account/repo, or a
  malicious push (or force-push) to whatever ref the URL points at — an
  attacker in that position can serve a malicious `install.sh`, compose
  file, SQL, or `models.tsv`. `models.tsv` is not inert data either: it's
  parsed directly (`IFS='|' read -r ...`) to drive the model lookup, so a
  tampered manifest is a tampered input to the script's own logic, not just
  tampered content.
- **`main` is a moving target.** Fetching from an unpinned branch means two
  installs a week apart are not reproducibly the same install — whatever is
  at the tip of `main` at fetch time is what you get.
- **`--owner` chooses whose code runs on your machine.** It selects the GHCR
  namespace both images are pulled from (`ghcr.io/<owner>/self-docs-*`). A
  mistyped or maliciously-suggested `--owner` pulls someone else's images,
  in full, with no cross-check against this repository's expected owner.
  Set it deliberately.
- **The `io.self-docs.*` label check is integrity, not authenticity.** It
  proves the pulled image is labeled for the model and dimension you asked
  for — that's what stops the stale-`0.0.x`-tag and dimension-mismatch
  problems described elsewhere in this doc and the
  [runbook](../docs/runbook.md#pre-built-container-images-ghcr). It does
  **not** prove who built the image. An attacker publishing their own image
  with matching `io.self-docs.embedding-model`/`io.self-docs.embedding-dim`
  labels passes this check cleanly — labels are metadata the image's
  builder sets, not a signature over the image's contents.
- **Pinning the fetch URL to a release tag (`--version`, or the tag in a
  tag-pinned `install.sh` download URL) is a real improvement, not
  cosmetic** — it closes the window where a later push to `main` changes
  the files an in-flight or future install fetches, and it's a prerequisite
  for checksums ever being meaningful (you can't publish a fixed checksum
  against a moving branch). It is **not sufficient by itself**: git tags
  can be moved or re-pushed by anyone with write access, so a tag pin
  narrows the trust problem, it doesn't remove it.

**What you can do about this today:**

- Read `install.sh` before you run it — that's the entire reason the
  documented flow is download-then-run rather than `curl | bash` (see the
  [README quickstart](../README.md#quickstart--pre-built-images-no-clone)).
  It's a single shell script with no external dependencies beyond
  `curl`/`docker`.
- Pass `--owner` explicitly and deliberately rather than accepting a
  copy-pasted default from somewhere you don't trust.
- Prefer `--source-dir` against a checkout you've already cloned and
  reviewed if you want to avoid the network-fetch path for
  `db/init/*.sql`/`docker-compose.yml`/`models.tsv` entirely.

**What does not exist today:** there is no checksum verification, no
signature verification, and no `@sha256:`-digest image pinning anywhere in
this kit — `install.sh` fetches files by URL and pulls images by tag, full
stop. If any of the following ever get implemented, treat them as
independent future hardening, not something already covered above: a
published SHA-256 per fetched file that `install.sh` verifies before use, a
switch from tag-based to `@sha256:`-digest image pulls, or a `BASE_URL`
pinned to a release tag by default instead of `main`.

## Flag reference

Every flag below is parsed verbatim in `install.sh`'s `while [[ $# -gt 0 ]]; do case "$1" in ...` block. Run `deploy/install.sh --help` to print the same list from the script itself.

| Flag | Argument | Default | What it does |
|------|----------|---------|---------------|
| `--dir <path>` | install directory | `./self-docs` | Where the rendered `.env`, `docker-compose.yml`, and `db/init/` are written. Must not exist-and-be-non-empty unless `--force` is also given. |
| `--model <name\|slug>` | model name or slug | the registry's `is_default=true` row (`bge-small-en-v1.5`) | Looked up against `models.tsv` by either the full HF model name or its slug; an unknown value fails with the valid-model list printed to stderr. |
| `--version <X.Y.Z>` | a version, optional `v` prefix | unset — floats the moving `<slug>` tag | Pins the image tag to `vX.Y.Z-<slug>` instead of the bare `<slug>` tag. Must match `^[0-9]+\.[0-9]+\.[0-9]+$` after stripping a leading `v`. |
| `--owner <ghcr-owner>` | GHCR namespace | `adamrussak` | Must match `^[a-z0-9][a-z0-9._-]*$` (lowercase GHCR namespace rules). |
| `--port-api <n>` | 1–65535 | `8080` | Host port for ingestion/`/admin`, bound to `127.0.0.1` only. Must differ from `--port-mcp` and must be free (checked with `lsof`, or a raw `/dev/tcp` probe if `lsof` isn't available). |
| `--port-mcp <n>` | 1–65535 | `8081` | Host port for the MCP endpoint, same rules as `--port-api`. |
| `--source-dir <path>` | a local checkout | unset — use the network | Read `deploy/models.tsv`, `deploy/docker-compose.yml`, and `db/init/*.sql` from `<path>` instead of fetching them over the network. `<path>` must look like a self-docs checkout (`<path>/deploy/models.tsv` must exist) or this fails immediately rather than silently falling back to the network. |
| `--dry-run` | — | off | Render `.env`, `db/init/*`, and `docker-compose.yml` into `--dir`, then stop. Zero `docker` invocations — no pull, no port/daemon checks. |
| `--no-start` | — | off | Render, `docker compose pull`, and verify labels, but do not `docker compose up`. Prints the exact command to run when ready. |
| `--no-verify-labels` | — | off | Skip the `io.self-docs.embedding-model`/`io.self-docs.embedding-dim` label check before starting. Only for images you already trust (e.g. a private mirror without labels) — see the stale-tag warning in the [README](../README.md#quickstart--pre-built-images-no-clone) and the [runbook](../docs/runbook.md#pre-built-container-images-ghcr) for why this check exists. |
| `--force` | — | off | Re-install into a non-empty `--dir`, rewriting `.env`, `docker-compose.yml`, and `db/init/*` there. **`POSTGRES_PASSWORD` is preserved, not regenerated, when the install's `pgdata` volume still exists** (checked with `docker volume inspect` against the project name `docker compose config` resolves for `--dir` — never guessed at by pattern-matching): Postgres only applies `POSTGRES_PASSWORD` when initializing an **empty** volume, so a fresh password paired with a surviving volume would otherwise lock you out with `FATAL: password authentication failed`. If the volume is confirmed gone (e.g. you already ran `down -v`), a new password is generated, same as a first install. If volume status can't be determined at all (`docker` missing, `--dry-run`), it preserves the old password as the safe default and says so. **`SYNC_TOKEN` and `MCP_TOKEN` are *not* volume-coupled and are always regenerated on `--force`** — update any MCP client's `Authorization: Bearer` header from the rewritten `.env` after a `--force` re-install. For a guaranteed-fresh `POSTGRES_PASSWORD` on a guaranteed-fresh volume: `docker compose down -v` (destroys the index) **before** re-installing with `--force`. |
| `-h`, `--help` | — | — | Print usage and exit `0`. |

Unrecognized flags (`*)` case) fail with exit `2` and a `(see --help)` hint.

## Exit codes

| Code | Meaning | Examples from the script |
|------|---------|---------------------------|
| `0` | Success, including `--help`, `--dry-run`, and `--no-start` completing their (partial) work as documented. | `print_help; exit 0`; end of `--dry-run` branch; end of `--no-start` branch. |
| `1` | Runtime failure — something that only fails once the script is actually trying to do network/docker work. | `die_runtime`: manifest fetch failed, `db/init/*` fetch failed, `docker-compose.yml` fetch failed, secret generation failed, `docker compose pull` failed, an image's `io.self-docs.*` labels are missing/mismatched, `docker compose up -d --wait` timed out or reported unhealthy. |
| `2` | Usage or preflight failure — something wrong before any real work started. | `die_usage`: unknown flag, missing required argument to a flag, invalid `--owner`/`--port-api`/`--port-mcp`/`--version`, unknown `--model`, `--source-dir` not a directory, `curl` missing when network access is required, install dir non-empty without `--force`, target parent not writable, a port already in use, Docker CLI/Compose v2/daemon not usable. |

`set -euo pipefail` is active for the whole script, so any unexpected/uncaught
command failure also exits non-zero (typically `1`, since `set -e` triggers
bash's default exit-code passthrough rather than `die_usage`'s explicit `2`).
