# Documentation Update Plan — self-docs

> **For the agent executing this plan.** This document is self-contained: it
> assumes no prior conversation. It is a **documentation-only** work order for
> the `self-docs` repository at `/Users/adam/code/self-docs` (branch `main`,
> baseline commit `e43ef54`). Every fact in the "Frozen fact sheet" below was
> verified against the code at that commit, with file references given so you
> can re-verify rather than trust.
>
> **Scope discipline:** you are updating documentation. You are **not** fixing
> code, tests, CI, or defaults. Three real code-level defects were found while
> preparing this plan; they are listed in §7 explicitly so you do **not** fix
> them.

---

## 1. Objective

Bring the repository's documentation up to date with the feature surface that
exists in the code today, and correct statements the code has since outrun.

Documents in scope:

| File | Role | Owning work item |
|---|---|---|
| `docs/adr/005-document-uploads-as-a-source-type.md` | new ADR | W1 |
| `docs/runbook.md` | operator source of truth (83 KB) | W2 (additive), W3 (corrections) |
| `README.md` | project front page | W4 |
| `docs/client-setup.md` | per-client MCP wiring | W5 |
| `skills/doc-cli/SKILL.md` | agent-facing CLI protocol | W5 |
| `AGENTS.md` | binding agent rules (`CLAUDE.md` is a **symlink** to it) | W6 |
| `design.md` | admin-UI style guide | W6 |
| `.env.example` | documented configuration surface | W7 |

Explicitly **out of scope**: `IMPLEMENTATION_PLAN.md`. It is a dated,
point-in-time design record (`Rev 2`, 2026-07-18) with its own review-resolution
history. Retrofitting current features into it destroys its value as a record.
This is a deliberate decision, not an oversight — do not "helpfully" update it.

---

## 2. Non-negotiable writing rules

1. **Present tense, current state.** Documentation describes how the system
   works *now*. It is not a changelog and not a release announcement.
2. **Banned phrasings** anywhere outside the ADR: "new in", "newly added",
   "recently", "we added", "we improved", "improvement", "now supports",
   "this release", and internal task ids in prose (`T7`, `T19_FIX`, `T11`).
   Write "An upload-type source is populated by…", never "We now support
   uploading…".
   - Grep the diff before you finish: `git diff | grep -inE "new in|recently|we (added|improved)|improvement|now supports|T[0-9]+_?[A-Z]*"`
3. **Two legitimate exceptions**, because they are operational instructions
   rather than product framing:
   - Migration sections ("apply `04_upload_sources.sql` to an existing
     database") — an operator genuinely needs to know a step is required.
   - `docs/adr/005-*.md` — an ADR's whole job is Context / Decision /
     Consequences / Alternatives. History belongs there and nowhere else.
4. **Corrections are made silently in place.** Do not annotate them ("changed
   from…", "previously this said…"). One exception, following the runbook's own
   existing convention: where an operator may have built automation against the
   old behavior, use the runbook's established superseded-guidance callout
   style. That applies to exactly one change in this plan — the admin form's
   `name` field becoming required (§3, "Corrections").
5. **One canonical location per fact.** Limits and constants are documented in
   depth in the runbook and the ADR. README, `client-setup.md`, and `AGENTS.md`
   **link** to those; they do not restate the numbers. This is what keeps the
   docs from drifting apart later.
6. **Never invent.** No endpoints, flags, env vars, or make targets that are not
   in the code. If something in this plan disagrees with the code, the **code
   wins** — note the discrepancy in your final report.
7. **Follow existing house style** in each file: heading depth, callout syntax
   (`> [!WARNING]`, `> [!IMPORTANT]`), table shapes, and voice. Read the
   surrounding sections before writing.

---

## 3. Frozen fact sheet (binding)

Every work item must use these facts and these names, exactly. Verified against
the code at `e43ef54`.

### 3.1 Schema and data model

- `doc_sources.source_type TEXT NOT NULL DEFAULT 'crawl'` with
  `CHECK (source_type IN ('crawl','upload'))`.
  - Defined for fresh installs in `db/init/01_schema.sql.template`.
  - Added to existing databases by `db/init/04_upload_sources.sql`, which is
    idempotent (`ADD COLUMN IF NOT EXISTS`, then
    `DROP CONSTRAINT IF EXISTS` → `ADD CONSTRAINT`).
  - Applied to a live database with:
    ```bash
    set -a; source .env; set +a
    ./scripts/migrate_uploads.sh          # CONTAINER=<name> to override self-docs-db
    ```
  - `db/init/*.sql` only runs against an **empty** Postgres data directory, so
    on an existing deployment the script above is required; on a fresh volume it
    runs automatically and re-running it by hand is a harmless no-op.
- An upload-type source's `base_url` is exactly the sentinel `upload://{name}`,
  enforced by `SourceConfig._base_url_matches_source_type`
  (`ingestion/app/config.py`). It is not a network address and is never fetched.
- Individual uploaded pages are stored under `upload://{name}/{rel_path}`
  (`store.ingest_uploaded_docs`).
- **Only a human, through the admin UI, can create an upload-type source.**
  `propose_doc_source` can never set `source_type='upload'`
  (`mcp-server/app/retrieval.py`, `ProposedSourceConfig`).

### 3.2 Accepted input and every limit

Sources: `ingestion/app/uploads.py`, `upload_pdf.py`, `upload_zip.py`,
`main.py`.

| Constraint | Value | Where enforced |
|---|---|---|
| Parser-registry formats | `.md`, `.markdown`, `.txt`, `.html`, `.htm`, `.pdf` | `uploads.ALLOWED_SUFFIXES` / `uploads.PARSERS` |
| Zip bundles | accepted, expanded **ahead of** the registry | `upload_zip.expand_zip`, called from `admin.py` |
| Request body cap | 50 MiB | `uploads.MAX_UPLOAD_BYTES` via `MaxBodySizeMiddleware` (`main.py`) |
| Aggregate in-flight cap | env `MAX_BODY_INFLIGHT_BYTES`, default `4 × MAX_UPLOAD_BYTES` (200 MiB) | `main.py` |
| Zip members | ≤ 500 | `uploads.MAX_ARCHIVE_MEMBERS` |
| Zip uncompressed total | ≤ 200 MiB | `uploads.MAX_UNCOMPRESSED_BYTES` |
| Zip per-member expansion ratio | ≤ 100 | `uploads.MAX_EXPANSION_RATIO` |
| Zip symlink members | rejected | `upload_zip._is_symlink` |
| Zip-slip / absolute / backslash / `..` paths | rejected | `uploads.normalize_rel_path` |
| PDF pages | ≤ 2000 | `upload_pdf.MAX_PDF_PAGES` |
| MCP upload content | ≤ 1 MB | `retrieval.upload_text` |
| MCP upload title | ≤ 200 chars | `retrieval.upload_text` |

Additional behavior worth documenting:

- Zip limits are checked from archive **metadata**, before any member is
  decompressed. Nested `.zip` members are skipped, never recursed. A single bad
  member fails **that member**, not the batch.
- `.zip` is deliberately **absent** from `uploads.ALLOWED_SUFFIXES` even though
  zip uploads work — that constant only covers suffixes dispatched through the
  `PARSERS` registry, and zips are expanded before dispatch. Any format list you
  write for users must include zip; make sure the two statements do not read as
  a contradiction.
- Encrypted and corrupt PDFs are rejected. Image-only/scanned PDFs produce no
  text and are rejected under `extract.MIN_EXTRACTED_LENGTH`.
- PDF-derived chunks have an **empty `heading_path`** (a PDF carries no markdown
  headings) — cosmetically the same case as the existing `pgvector-readme`
  source. `AGENTS.md` already documents that empty-`heading_path` caveat for
  README-derived sources; extend it rather than duplicating it.
- `upload_doc_text` slugifies `title` into the page URL, so **re-uploading the
  same title replaces that page** instead of duplicating it. Exceeding either
  cap rejects the whole call with nothing partially processed.

### 3.3 The three write paths

1. **Admin UI.**
   - Create form (`GET/POST /admin/sources/new`) has a source-type radio:
     `Crawled URL` (default) / `Uploaded files`. Choosing `Uploaded files`
     hides `base_url` (it is synthesized as the sentinel) and reveals an
     **optional** file input. Attaching nothing is supported and expected —
     "create the source now, upload later".
   - Edit page of an upload-type source carries a standing upload form →
     `POST /admin/sources/{id}/upload` (multipart, one or more `files` parts).
2. **CLI.**
   ```bash
   make upload SOURCE=my-docs PATH=./some/docs/dir
   make upload SOURCE=my-docs PATH=./manual.pdf
   ```
   Wraps `scripts/upload_docs.py`, which walks directories recursively, batches
   at ≤ 20 files / 40 MB per request to stay under the server cap, logs in with
   `SYNC_TOKEN` via `/admin/login` (session cookie + CSRF, same flow as
   `scripts/push_sources.py`), resolves `--source NAME` to a numeric id by
   parsing the rendered `/admin` HTML, and posts each batch. Flags: `--url`,
   `--token`, `--continue-on-error`.
3. **MCP tool.** `upload_doc_text(source, title, content)` — writes a page of
   Markdown/plain text into an **existing** upload-type source, addressed by
   name. It can never create a source and never writes into a crawl-type
   source; both are rejected with a clear reason, as is an unknown name.

### 3.4 Ingestion semantics (`store.ingest_uploaded_docs`)

- Parsed markdown goes through the **same** path a crawl uses:
  `chunker.chunk_markdown` → `embedder.embed_chunks` → `replace_page`, with the
  same `content_hash` comparison, so unchanged content is skipped without
  re-embedding.
- **Raw uploaded bytes are never persisted** — not to disk, not to the database.
  Only parsed markdown and its chunks are stored. (The ASGI multipart parser may
  transiently spool a large part to an OS temp file, cleaned up at request end;
  no application code writes uploaded bytes anywhere.)
- **Never** deletes pages absent from a batch. A batch is not a complete
  enumeration of the source's pages, so absence is not evidence of removal.
  Pages from earlier batches survive untouched.
- Status via `classify_sync`: nothing indexed → `failed`; some indexed with at
  least one failure → `partial`; otherwise `ok`.
- It **does** call `metrics.record_sync_outcome(...)` and update
  `doc_sources.last_status` / `last_synced` — upload sources therefore appear in
  `/metrics` exactly like crawl sources. See §3.6 for the alerting consequence.
- It takes the **same single sync lock** as a crawl. A running sync blocks an
  upload and vice versa; the operator sees "another sync/upload is already in
  progress; try again shortly." In production, ingestion runs in a background
  thread and the operator is redirected with an `upload_started` banner;
  progress streams into the same `/status` widget crawl syncs use.

### 3.5 Guards

Every crawl entrypoint refuses or skips upload-type sources with an explicit
message:

| Entrypoint | Behavior for an upload source |
|---|---|
| `POST /sync` (targeted) | refused with an explanatory message |
| `POST /sync` (unscoped sweep) | filtered out before crawling |
| `store.sync_all` | skipped, logs `sync_all_skipped_upload_source` |
| Admin per-source **Sync** button | refused: no URL to crawl, use the upload form |
| Admin **Refresh** (purge + recrawl) | refused: there is no crawl to re-populate it — purge, then re-upload |
| Scheduler (`due_sources`) | upload rows excluded by the query; never scheduled |
| Admin **Purge** | **applies** — clears the source's pages/chunks |

### 3.6 Retrieval and observability surface

- `list_doc_sources` (MCP) returns a `source_type` column alongside `source`,
  `last_synced`, `last_status`, `chunks`.
- `search_docs` renders an uploaded page's `upload://…` URL as **plain text**,
  not a link, because the scheme is not `http(s)`.
- Alerting (`ops/alerts/ingestion.yml`), given §3.4's metrics behavior:
  - `SourceSyncStale` (48 h without a successful sync) **will fire** for an
    upload source nobody has uploaded to. That is expected and means "no one has
    uploaded recently", not "a crawl is broken".
  - `SourceIndexedNothing` for an upload source means a batch finished having
    indexed nothing — every document failed to parse or index.
  - `SoftFailRatioHigh` and `ShellSuspectedRatioHigh` cannot apply: uploads
    produce no soft failures and no JS-shell detection.

### 3.7 Admin banner convention

`ingestion/app/admin.py` (`_message_level`, `_ALLOWED_MESSAGE_LEVELS`,
`_level_suffix`, `_SUCCESS_SYNC_STATUSES`):

- Severity is carried **explicitly** by the redirecting route as `?level=`,
  never inferred from the message text.
- `?level=` is whitelisted to `warning`. Anything else — absent, mis-cased,
  attacker-supplied — collapses to the default success styling.
- Green success styling is reserved for `status='ok'`. `partial` and `failed`
  both render amber, and an unrecognized future status deliberately falls on the
  amber side.

### 3.8 Corrections to text that is currently wrong

1. **The admin create form now requires `name`** (`Form(...)` in
   `create_source_submit`; `required pattern="^[a-z0-9-]+$"` in
   `templates/admin/form.html`). The runbook currently says you may leave it
   blank and the server will derive it. Derivation **still applies** to
   `include_prefixes` and `max_pages` on that form
   (`source_defaults.apply_creation_defaults`), and `propose_doc_source`
   remains fully URL-only with `name` derived. This is the one change that gets
   a superseded-guidance callout (rule 4 in §2) — an operator may have
   automation posting a blank name.
2. **Embedding default.** The registry default in `config/models.yaml` is
   `BAAI/bge-small-en-v1.5` (384-dim), which is also what `.env.example` ships
   and what `db/init/01_schema.sql` creates (`vector(384)`).
   `ingestion/app/embedder.py`, `mcp-server/app/retrieval.py`, and
   `docker-compose.yml` still fall back to `mixedbread-ai/mxbai-embed-large-v1`
   (1024-dim). **That mismatch is exactly what README's "Known issue" block
   describes.** Documentation must tell this story **one way**: the default is
   bge-small/384, and the code-level fallbacks are the known defect. Do not
   leave a second, contradictory "the default is mxbai (1024-dim)" statement
   anywhere. The README Known-issue block itself is still accurate — leave it
   verbatim.
3. **CI migration coverage.** `.github/workflows/test.yml` and `release.yml`
   apply `db/init/01_schema.sql` and `02_sources_config.sql` only — not `03` or
   `04`. The runbook's claim that CI exercises the live-migration path must be
   narrowed to those two files.
4. **`doc-cli` default API URL.** The binary defaults to
   `http://localhost:8000` (`cli/cmd/root.go`, `cli/internal/api/client.go`)
   while the compose stack publishes ingestion on `127.0.0.1:8080`.
   `docs/client-setup.md` already tells users to export
   `SELF_DOCS_API_URL=http://localhost:8080`; `skills/doc-cli/SKILL.md` states
   the `8000` default with no such note. Make the two agree and state plainly
   that the variable must be set for the standard stack.
5. **Admin data ops.** `POST /purge`, `POST /refresh`, `POST /stop` and their
   `make purge` / `make refresh` / `make stop` targets exist and are only
   mentioned in the runbook's exposure table — they have no operational
   documentation. The admin UI's documented "Full CRUD + workflow surface" list
   also omits them, the upload form, and the upload-source refusals.
6. **Uploaded content is not re-crawlable.** The runbook's backup, restore, and
   nuke-and-rebuild sections all rest on "the corpus is fully re-crawlable from
   upstream". That is **false** for upload-type sources: `docker compose down -v
   db`, a purge, or restoring an older dump loses uploaded content permanently
   unless the operator still holds the original files. A `pg_dump` is the only
   backup of an upload source's content.
7. **MCP tool count.** Four tools are exposed — `search_docs`,
   `list_doc_sources`, `propose_doc_source`, `upload_doc_text`
   (`mcp-server/app/server.py`). `docs/client-setup.md` says two, in the
   overview and in all three per-client Verify steps.
8. **Undocumented make targets.** README's Development section omits `upload`,
   `purge`, `refresh`, `stop`, `reindex`, `test-db-up`, `test-db-down`,
   `test-db-reset`.
9. **Missing from README's Architecture.** The optional headless `renderer`
   compose service and the upload path are absent from the diagram, the prose,
   and the layer table.

---

## 4. Work items

Ordered for sequential execution. If you parallelize across subagents, respect
the dependency column — W2 and W3 touch the same file and must not run
concurrently.

### W1 — New ADR: uploads as a source type
**File:** `docs/adr/005-document-uploads-as-a-source-type.md` (new; use exactly
this path — other work items link it)
**Depends on:** nothing

Read `docs/adr/003-llms-txt-etag-multilang-fts.md` and
`docs/adr/004-selectable-embedding-models.md` first and match their section
order, depth, and voice.

Record:
- **Decision:** uploads modeled as a `source_type` column on `doc_sources`
  rather than a separate table, so one row, one status, one metrics series, and
  one retrieval path serve both kinds of source.
- The `upload://{name}` sentinel `base_url`, and why a non-`http(s)` sentinel
  was preferred over making `base_url` nullable (every existing NOT NULL
  constraint, join, and display path keeps working; validation splits cleanly
  per `source_type`).
- Parsed-markdown-only persistence: raw bytes never reach disk or the database.
- Reuse of the crawl chunker/embedder/hash-diff path instead of a parallel
  pipeline.
- No stale-page deletion for upload batches, and why (a batch is not an
  enumeration).
- Human-only creation; `propose_doc_source` can never mint an upload source.
- **Consequences** must state explicitly that uploaded content is
  unrecoverable after a purge or nuke-and-rebuild unless the operator still
  holds the original files, and that a `pg_dump` is its only backup.
- **Alternatives rejected:** a separate `uploaded_docs` table; an on-disk or
  object blob store for raw bytes; a dedicated upload service.

**Acceptance:**
- File exists at exactly that path, numbered `005`, `Status: Accepted`.
- Every factual claim traces to `ingestion/app/uploads.py`,
  `upload_pdf.py`, `upload_zip.py`, `store.ingest_uploaded_docs`,
  `ingestion/app/config.py`, or `db/init/04_upload_sources.sql`.
- The unrecoverability consequence is stated explicitly.
- No other file touched.

---

### W2 — Runbook: add upload coverage (additive only)
**File:** `docs/runbook.md`
**Depends on:** nothing
**Constraint:** **add** sections; do not modify or reflow any existing line.
W3 owns all in-place edits to this file.

(a) **New top-level `## Upload sources`**, placed after `## Add a new doc
source` and before `## Admin UI`. Cover, in this order:
- what an upload-type source is, and how it differs from a crawl source
  (sentinel `base_url`, no crawl, human-only creation);
- creating one in the admin UI (source-type radio, `name` required, `base_url`
  synthesized, files optional at create);
- all three write paths (§3.3) with copy-pasteable commands;
- accepted formats (§3.2), including zip, with the registry-vs-zip nuance
  stated so it does not read as a contradiction;
- every limit and every rejection reason, as a table;
- ingestion semantics (§3.4): shared chunker/embedder, hash-diff skip, no
  stale-page deletion, the shared sync lock, background ingestion and the
  `/status` widget;
- the guard matrix (§3.5), including what **Purge** means for an upload source.

(b) **New `### Apply the upload-sources migration`** inside the existing
`## REQUIRED — apply the doc_sources config migration (read this first)`
section, covering `db/init/04_upload_sources.sql` and
`scripts/migrate_uploads.sh`. Match that section's established framing:
idempotency, fresh-volume vs. live-database behavior, and the
"is this already applied here?" guidance.

(c) **New troubleshooting entries** under `## Troubleshooting`:
- upload rejected — a per-limit table mapping the operator-visible error string
  to the limit that produced it;
- "another sync/upload is already in progress";
- an upload source reporting `last_status='failed'` (what to check, given no
  crawl is involved);
- uploaded content missing after a purge or nuke-and-rebuild → it is gone;
  re-upload from the original files.

**Acceptance:**
- Every new heading is anchor-stable and linkable (`#upload-sources`, etc.) —
  W3 and W4 link to them.
- Every command runs as written against the local stack.
- No existing line modified (`git diff` shows additions only, plus whatever
  whitespace a clean insertion requires).
- Constants match §3.2 exactly.
- No changelog voice; no `T7` / `T19_FIX` in prose.

---

### W3 — Runbook: correct existing sections
**File:** `docs/runbook.md`
**Depends on:** **W2** (same file — run after it)

1. `## Add a new doc source`, section **A**: `name` is now required (§3.8.1).
   Keep the `include_prefixes` / `max_pages` derivation text, and keep section
   **B**'s URL-only `propose_doc_source` path intact. Use the runbook's existing
   superseded-guidance callout convention here. Add the source-type selector to
   the creation steps, linking W2's `## Upload sources`.
2. `## Admin UI` → **Full CRUD + workflow surface**: add Purge, Refresh
   (purge + recrawl), Stop (`POST /purge`, `/refresh`, `/stop` and their make
   targets), the upload form on an upload-type source's edit page, and the
   explicit refusals for upload sources on Sync/Refresh. Add the banner
   convention (§3.7).
3. `## Re-index from scratch (nuke-and-rebuild)`, `## Backup`, `## Restore`:
   correct the "fully re-crawlable" premise per §3.8.6. State plainly that
   `down -v`, a purge, or restoring an older dump permanently loses uploaded
   content, and that a dump is the only backup of an upload source's content.
4. `## Switch the embedding model`: registry default is
   `BAAI/bge-small-en-v1.5` (384-dim) — §3.8.2. And in the migration section,
   narrow the CI claim to `01_schema.sql` + `02_sources_config.sql` (§3.8.3).
5. `## The scheduler`: upload sources are excluded from `due_sources` and are
   never scheduled.
6. `## Alerting — Prometheus rules`: per §3.6, note on each affected alert that
   upload sources do emit sync metrics — `SourceSyncStale` firing for an upload
   source means nobody has uploaded recently (expected, not a crawl fault);
   `SourceIndexedNothing` means a batch indexed nothing; `SoftFailRatioHigh`
   and `ShellSuspectedRatioHigh` cannot apply.

**Acceptance:**
- No statement in the file contradicts §3 or W2's new section.
- Every internal anchor still resolves after edits.
- The embedding-model text agrees with README's Known issue instead of
  asserting a second "default".
- No changelog voice introduced.

---

### W4 — README
**File:** `README.md`
**Depends on:** nothing

(a) Lede and `## Why self-docs`: the pipeline indexes crawled sites **and**
uploaded documents (Markdown/text, HTML, PDF, zip bundles). Fix the embedding
bullet per §3.8.2 and point at the existing Known issue for the code-level
fallbacks — do not leave two conflicting "defaults" in one file. Mention the
optional headless renderer alongside llms.txt / conditional GET.

(b) `## Architecture`: add the `renderer` service and the upload path to the
ASCII diagram; add source types / uploads to the prose beneath it and to the
layer table.

(c) `## MCP Tools & REST Endpoints`: add
`upload_doc_text(source, title, content)` with its caps and its
existing-upload-source-only constraint. Note that `list_doc_sources` reports
`source_type` and that `upload://` URLs render as plain text.

(d) `## Managing Sources`: add rows for creating an upload source (admin UI) and
populating it (edit-page form, `make upload SOURCE=<name> PATH=<file_or_dir>`,
`upload_doc_text`).

(e) `## Development`: document `make upload`, `purge`, `refresh`, `stop`,
`reindex`, `test-db-up`, `test-db-down`, `test-db-reset` alongside the existing
targets.

(f) `## Contents`: add any new heading; link W2's runbook `#upload-sources`
section and `docs/adr/005-document-uploads-as-a-source-type.md`. Add ADR-005 to
the Documentation table row if ADRs are itemized there.

Leave the `## Known issue — a fresh install with default .env values is broken`
block **verbatim** — it is still accurate.

**Acceptance:**
- Every anchor in `## Contents` resolves.
- The ASCII diagram stays aligned in a monospace render.
- No claim contradicts `docs/runbook.md` or §3.
- README no longer states mxbai as *the default* anywhere outside the Known
  issue block.

---

### W5 — Client setup and the doc-cli skill
**Files:** `docs/client-setup.md`, `skills/doc-cli/SKILL.md`
**Depends on:** nothing

`docs/client-setup.md`:
- Fix "Two tools are exposed" and **all three** per-client Verify steps (Cursor,
  Claude Code, Antigravity) to list `search_docs`, `list_doc_sources`,
  `propose_doc_source`, `upload_doc_text`.
- Add a short **Uploading documents from an agent** subsection covering
  `upload_doc_text`'s contract — existing upload-type source only, 1 MB content
  / 200-char title caps, same-title replaces the page, never creates a source —
  linking the runbook's `#upload-sources` section rather than restating limits.
- Add a troubleshooting bullet for a rejection because the target source is not
  upload-type.

`skills/doc-cli/SKILL.md`:
- Note that hits from upload-type sources carry `upload://{source}/{path}`
  URLs, which are identifiers, not fetchable links.
- Resolve the API-URL discrepancy (§3.8.4): state the built-in default and that
  `SELF_DOCS_API_URL=http://localhost:8080` is required for the standard compose
  stack, agreeing with `client-setup.md`.
- Do **not** change `doc-cli`'s flags or invent subcommands. The real surface is
  `search`, `get`, `tree`, `skill install`, `skill status`, with global flags
  `--url`, `--token`, `--json`, `--compact`, `--limit`, `--verbose`.

**Acceptance:**
- Tool lists match `mcp-server/app/server.py` exactly.
- API-URL guidance agrees across both files.
- No fabricated CLI flags or subcommands.
- No changelog voice.

---

### W6 — Agent rules and the style guide
**Files:** `AGENTS.md` (**edit this one** — `CLAUDE.md` is a symlink to it),
`design.md`
**Depends on:** nothing

`AGENTS.md` — the memory-boundary rules predate uploads and must now cover a
write path agents actually have. Keep the existing numbered structure and voice;
do not rewrite unrelated rules; touch the `doc-cli` protocol section only as far
as accuracy requires. Add/extend so that:
1. `list_doc_sources`'s `source_type` column is named as how an agent
   distinguishes a crawl source from an upload source.
2. `upload_doc_text` is documented as the **only** agent-facing write into the
   corpus: usable solely against an existing, human-created upload-type source,
   never able to create one — and **still bound by the Mem0 boundary**.
   Uploaded content must be static framework/library reference. Decisions, task
   notes, PR context, and project state stay in Mem0 and must never be
   uploaded. This tightens rule 2 ("never store project state in the docs
   index") rather than loosening it.
3. The citation rule notes that upload-derived hits carry `upload://`
   identifiers rather than URLs, and that PDF-derived chunks may have an empty
   `heading_path` (extend the existing `pgvector-readme` caveat) — so cite
   source name + `heading_path`.

`design.md` — add message/banner states to `## Components`, per §3.7: severity
is explicit and whitelisted, green success styling is reserved for
`status='ok'`, and an unrecognized outcome renders amber rather than green.

**Acceptance:**
- `AGENTS.md` keeps its numbered structure and stays internally consistent — no
  rule contradicting another.
- The Mem0-vs-docs boundary is strengthened, not loosened.
- The `design.md` addition matches actual behavior in `ingestion/app/admin.py`
  and `templates/admin/base.html`.
- No changelog voice.

---

### W7 — Document the upload knob in `.env.example`
**File:** `.env.example`
**Depends on:** nothing

`.env.example` currently has no upload entries at all. Add a commented block, in
the file's existing comment style and grouping, for `MAX_BODY_INFLIGHT_BYTES`:
the aggregate in-flight request-body cap, default `4 × MAX_UPLOAD_BYTES`
(200 MiB), read in `ingestion/app/main.py`. Explain what raising or lowering it
does, and that the per-request 50 MiB cap is a code constant, not an env var.

Do **not** add any variable the code does not read, change any existing default,
or uncomment anything.

**Acceptance:**
- `grep MAX_BODY_INFLIGHT_BYTES ingestion/app/main.py` confirms the name and
  default.
- Copying `.env.example` to `.env` and running `make up` behaves identically to
  before.
- Only `.env.example` is touched.

---

## 5. Final verification pass

Run this before reporting done. Report `file:line` for anything that fails.

1. **Fact accuracy.** Every constant, limit, route, env var, make target, tool
   name, and CLI flag you documented exists as documented. Spot-check against
   `ingestion/app/uploads.py`, `upload_pdf.py`, `upload_zip.py`, `admin.py`,
   `main.py`, `store.py`, `mcp-server/app/server.py`, `Makefile`,
   `db/init/04_upload_sources.sql`, `ops/alerts/ingestion.yml`,
   `cli/cmd/*.go`.
2. **No changelog voice.**
   ```bash
   git diff | grep -inE "new in|newly|recently|we (added|improved)|improvement|now supports|this release|T[0-9]+_[A-Z]+"
   ```
   Hits inside `docs/adr/005-*.md` are acceptable; anywhere else, fix.
3. **Consistency.** The embedding-default story is told one way across README
   and runbook. `doc-cli` API-URL guidance agrees between `client-setup.md` and
   `SKILL.md`. Upload limits agree across runbook, README, `client-setup.md`,
   and ADR-005. `AGENTS.md` contains no rule contradicting another.
4. **Links and anchors.** Every relative link and every `#anchor` in the changed
   files resolves — including README's `## Contents` list and all cross-document
   links into the new runbook section and ADR-005.
5. **Omissions.** Walk §3 end to end: is any fact undocumented in the place an
   operator or an agent would actually look for it?

---

## 6. Ground truth — files to read before writing

| Topic | Read |
|---|---|
| Upload parsing, limits, path safety | `ingestion/app/uploads.py` |
| PDF parsing | `ingestion/app/upload_pdf.py` |
| Zip expansion and its security checks | `ingestion/app/upload_zip.py` |
| Admin routes, upload routes, banners, data ops | `ingestion/app/admin.py` |
| Body-size middleware, `/sync` `/purge` `/refresh` `/stop` `/status`, `/api/v1/*` | `ingestion/app/main.py` |
| `ingest_uploaded_docs`, `sync_all` guards, `classify_sync` | `ingestion/app/store.py` |
| `SourceConfig`, `source_type` validation | `ingestion/app/config.py` |
| Derived creation defaults | `ingestion/app/source_defaults.py` |
| Sync metrics | `ingestion/app/metrics.py` |
| MCP tools and their docstrings | `mcp-server/app/server.py` |
| `list_sources`, `upload_text`, proposal validation | `mcp-server/app/retrieval.py` |
| Admin form / upload form markup | `ingestion/app/templates/admin/form.html` |
| Migration SQL and script | `db/init/04_upload_sources.sql`, `scripts/migrate_uploads.sh` |
| Upload CLI | `scripts/upload_docs.py` |
| Make targets | `Makefile` |
| Alert rules | `ops/alerts/ingestion.yml` |
| Compose services and profiles (`renderer`) | `docker-compose.yml`, `docker-compose.prod.yml` |
| CLI defaults and subcommands | `cli/cmd/root.go`, `cli/internal/api/client.go` |

---

## 7. Known code-level defects — DO NOT FIX

Found while preparing this plan. They are real, and they are **out of scope**.
Documentation should describe reality accurately (including warning about these
where the runbook and README already do), but do not change code, tests, CI, or
defaults to "resolve" them.

1. **CI never exercises the `03`/`04` migrations.**
   `.github/workflows/test.yml` and `release.yml` apply `01_schema.sql` and
   `02_sources_config.sql` only, so `04_upload_sources.sql` and
   `scripts/migrate_uploads.sh` have no CI coverage on the live-migration path.
   W3 narrows the runbook's claim; the gap itself stays.
2. **Embedding-default drift is still open.**
   `embedder.DEFAULT_MODEL_NAME` / `DEFAULT_EMBEDDING_DIM`,
   `retrieval.DEFAULT_MODEL_NAME`, and `docker-compose.yml` still name
   mxbai/1024 against a 384-dim registry default and a `vector(384)` schema. Two
   registry-drift tests are expected-red because of it
   (`mcp-server/tests/test_registry_defaults.py::test_retrieval_defaults_match_registry_default`,
   `tests/test_model_registry.py::test_ingestion_embedder_defaults_match_registry_default`).
   Documentation keeps warning; nothing gets fixed here.
3. **`doc-cli`'s default `--url` is `http://localhost:8000`** while ingestion
   publishes `8080`, so the binary's zero-config default cannot work against the
   standard stack. W5 documents the required `SELF_DOCS_API_URL`; changing the
   default is a code decision, not a docs one.
