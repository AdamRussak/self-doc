-include .env
export

.PHONY: up down up-prod down-prod sync test eval lint typecheck configure reindex backup backup-prune backup-auto restore

# Select the embedding model from config/models.yaml. Resolves the model's
# vector dimension and per-service memory limits, writes them into .env, and
# renders db/init/01_schema.sql. No MODEL => the registry default.
# Usage: make configure                              (default model)
#        make configure MODEL=BAAI/bge-base-en-v1.5  (a specific model)
configure:
	python3 scripts/configure_model.py "$(MODEL)"

# Re-embed the entire corpus with the currently-configured model. Required
# after `make configure` changes the model (content-hash change-detection would
# otherwise skip unchanged pages and leave stale/mismatched vectors). Truncates
# the crawled pages/chunks (NOT doc_sources) then triggers a fresh sync.
reindex:
	@echo "Truncating doc_pages/doc_chunks (sources preserved) then re-syncing..."
	docker compose exec -T db psql -U $${POSTGRES_USER} -d $${POSTGRES_DB} \
		-c "TRUNCATE doc_pages, doc_chunks RESTART IDENTITY CASCADE;"
	$(MAKE) sync

# Bring up the full stack locally (db + ingestion + mcp-server) using loopback ports.
up:
	docker compose --profile full up -d

down:
	docker compose down

# Bring up the full stack in production/home-lab with Traefik ingress routing.
up-prod:
	docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile full up -d

down-prod:
	docker compose -f docker-compose.yml -f docker-compose.prod.yml down

# Trigger a documentation sync via the ingestion service's /sync endpoint.
# Reads SYNC_TOKEN from .env automatically via -include above.
sync:
	curl -sS -X POST http://localhost:8080/sync \
		-H "Authorization: Bearer $(SYNC_TOKEN)" \
		-H "Content-Type: application/json"

# Runs the full suite for BOTH packages (ingestion, mcp-server) plus the
# cross-package e2e test, as the single `make test` entrypoint from repo root.
#
# Each package ships its own `app` top-level package name, so they cannot
# share one venv/import namespace; each gets its own venv (created here if
# missing) and is tested with its own interpreter. The e2e test spans both by
# shelling out to each venv's interpreter in turn (see tests/test_e2e.py).
#
# DB-dependent tests (ingestion/tests/test_store.py, mcp-server/tests/
# test_retrieval_integration.py, tests/test_e2e.py) need the compose `db` up
# and reachable on 127.0.0.1:5433 with POSTGRES_USER=self_docs
# POSTGRES_PASSWORD=testpass123 POSTGRES_DB=self_docs (or override via env).
#
# The base docker-compose.yml deliberately publishes NO db port (security
# review finding M1: db must be reachable only inside the compose network in
# production). The host-loopback mapping is an explicit opt-in test overlay —
# bring db up (with the port) first via:
#     docker compose -f docker-compose.yml -f docker-compose.test.yml up -d db
# They skip cleanly (not fail) when no db is reachable, so `make test` stays
# green without Docker too, but full coverage requires the db up as above.
test:
	@echo "=== ensuring ingestion/.venv ==="
	@test -d ingestion/.venv || python3 -m venv ingestion/.venv
	@ingestion/.venv/bin/pip install -q -U pip
	@ingestion/.venv/bin/pip install -q -e ingestion
	@ingestion/.venv/bin/pip install -q pytest
	@echo "=== ensuring mcp-server/.venv ==="
	@test -d mcp-server/.venv || python3 -m venv mcp-server/.venv
	@mcp-server/.venv/bin/pip install -q -U pip
	@mcp-server/.venv/bin/pip install -q -e mcp-server
	@mcp-server/.venv/bin/pip install -q pytest pyyaml defusedxml
	@ingestion/.venv/bin/pip install -q pytest-cov
	@mcp-server/.venv/bin/pip install -q pytest-cov
	@echo "=== ingestion test suite ==="
	cd ingestion && ../ingestion/.venv/bin/pytest -q --cov=app --cov-report=term-missing:skip-covered
	@echo "=== mcp-server test suite ==="
	cd mcp-server && ../mcp-server/.venv/bin/pytest -q --cov=app --cov-report=term-missing:skip-covered
	@echo "=== e2e (cross-package) test suite ==="
	cd tests && ../ingestion/.venv/bin/python -m pytest -q
	@echo "make test: all suites green (DB-dependent tests skip cleanly if 'docker compose up -d db' wasn't run first)."

# Run retrieval quality evaluation against a synced database.
# Requires: compose db up with synced seed sources.
# Skips cleanly if no db is reachable.
eval:
	@echo "=== ensuring mcp-server/.venv (eval needs retrieval module) ==="
	@test -d mcp-server/.venv || python3 -m venv mcp-server/.venv
	@mcp-server/.venv/bin/pip install -q -U pip
	@mcp-server/.venv/bin/pip install -q -e mcp-server
	@mcp-server/.venv/bin/pip install -q pytest pyyaml psycopg[binary]
	@echo "=== retrieval quality eval ==="
	cd tests/eval && ../../mcp-server/.venv/bin/python -m pytest -q -m eval

# Tooling venv for lint/typecheck (ruff + mypy). Kept separate from the two
# package venvs; ruff is a standalone binary, mypy runs with
# --ignore-missing-imports so it needn't install every runtime dependency.
TOOLS_VENV = .tooling-venv
$(TOOLS_VENV):
	python3 -m venv $(TOOLS_VENV)
	@$(TOOLS_VENV)/bin/pip install -q -U pip ruff mypy

# Lint across both packages, scripts, and tests. (Formatting is available via
# `ruff format` but intentionally NOT gated — this codebase uses deliberate
# hand-alignment in its long explanatory comments/tables.)
lint: $(TOOLS_VENV)
	$(TOOLS_VENV)/bin/ruff check .

# Static type-check the application code and scripts. Each package is checked
# separately (both use a top-level `app` package, so a single invocation would
# see two modules named `app`). mypy.ini quarantines the pre-existing typing
# backlog so the gate enforces types on new/changed code.
typecheck: $(TOOLS_VENV)
	cd ingestion && MYPYPATH=. ../$(TOOLS_VENV)/bin/mypy --config-file ../mypy.ini app
	cd mcp-server && MYPYPATH=. ../$(TOOLS_VENV)/bin/mypy --config-file ../mypy.ini app
	$(TOOLS_VENV)/bin/mypy --config-file mypy.ini scripts

# Dump the docs database to a timestamped custom-format archive under ./backups.
backup:
	mkdir -p backups
	docker compose exec -T db pg_dump -U $${POSTGRES_USER} -d $${POSTGRES_DB} -Fc \
		> backups/docs_$$(date +%Y%m%d_%H%M%S).dump
	@echo "Backup written to backups/docs_<timestamp>.dump"

# Prune old backups, keeping the most recent KEEP (default 4) dumps.
KEEP ?= 4
backup-prune:
	@echo "Keeping the $(KEEP) most recent backups, removing older ones..."
	@cd backups 2>/dev/null && ls -1t docs_*.dump 2>/dev/null | tail -n +$$(($(KEEP)+1)) | xargs -r rm -v || true

# Combined target for cron/timer: backup then prune.
backup-auto: backup backup-prune

# Restore from a backup produced by `make backup`.
# Usage: make restore FILE=backups/docs_20260101_030000.dump
restore:
	@if [ -z "$(FILE)" ]; then echo "Usage: make restore FILE=backups/docs_<timestamp>.dump"; exit 1; fi
	cat $(FILE) | docker compose exec -T db pg_restore -U $${POSTGRES_USER} -d $${POSTGRES_DB} --clean --if-exists
	@echo "Restore complete. pg_dump preserves the HNSW index definition but not"
	@echo "its build — run REINDEX INDEX doc_chunks_embedding_idx; inside the db"
	@echo "container to rebuild it (can take a while on a large corpus)."
