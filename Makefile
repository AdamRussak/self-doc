-include .env
export

PREFIX ?= $(HOME)/.local

# Single source of truth for the isolated `db-test` service's connection
# settings. `export` above pushes these into every recipe's environment, so
# BOTH docker-compose.test.yml (which reads ${TEST_POSTGRES_USER:-...} etc.,
# falling back to the same defaults) and the `test` target's pytest
# invocations resolve them identically — the container and the test suites
# cannot drift apart. Override on the command line (e.g.
# `make test TEST_POSTGRES_PORT=5434`) if 5433 is already in use locally.
TEST_POSTGRES_HOST ?= 127.0.0.1
TEST_POSTGRES_PORT ?= 5433
TEST_POSTGRES_USER ?= self_docs
TEST_POSTGRES_PASSWORD ?= testpass123
TEST_POSTGRES_DB ?= self_docs

.PHONY: up down up-prod down-prod sync test test-db-up test-db-down test-db-reset build-cli test-cli install-cli install-skill install eval lint typecheck configure reindex backup backup-prune backup-auto restore purge refresh stop

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

# Build the doc-cli Go executable in the project root.
build-cli:
	cd cli && go build -o ../doc-cli .

# Run Go unit and API client tests.
test-cli:
	cd cli && go test -v ./...

# Install doc-cli executable to $(PREFIX)/bin.
install-cli: build-cli
	@mkdir -p $(PREFIX)/bin
	cp doc-cli $(PREFIX)/bin/doc-cli
	@chmod +x $(PREFIX)/bin/doc-cli
	@echo "Installed doc-cli binary to $(PREFIX)/bin/doc-cli"

# Install doc-cli AI agent skill globally to ~/.gemini/config/skills/doc-cli/SKILL.md.
install-skill: build-cli
	./doc-cli skill install --global --force

# Combined installation: install binary to PATH and register AI agent skill.
install: install-cli install-skill


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

# Purge a specific source by id/name. Usage: make purge SOURCE=1 or make purge SOURCE=python-sdk
purge:
	@if [ -z "$(SOURCE)" ]; then echo "Usage: make purge SOURCE=<id_or_name>"; exit 1; fi
	curl -sS -X POST http://localhost:8080/purge \
		-H "Authorization: Bearer $(SYNC_TOKEN)" \
		-H "Content-Type: application/json" \
		-d '{"source": "$(SOURCE)"}'

# Refresh a specific source by id/name. Usage: make refresh SOURCE=1 or make refresh SOURCE=python-sdk
refresh:
	@if [ -z "$(SOURCE)" ]; then echo "Usage: make refresh SOURCE=<id_or_name>"; exit 1; fi
	curl -sS -X POST http://localhost:8080/refresh \
		-H "Authorization: Bearer $(SYNC_TOKEN)" \
		-H "Content-Type: application/json" \
		-d '{"source": "$(SOURCE)"}'

# Stop the running sync. Usage: make stop
stop:
	curl -sS -X POST http://localhost:8080/stop \
		-H "Authorization: Bearer $(SYNC_TOKEN)" \
		-H "Content-Type: application/json"

# Bring up the isolated `db-test` service (own container, own `pgdata_test`
# volume, schema applied from db/init/) on 127.0.0.1:5433. NEVER the
# production `db` service/volume — see docker-compose.test.yml and
# docs/runbook.md, "Isolated test database (db-test)".
test-db-up:
	@# The container_name below (self-docs-db-test) is fixed, so a db-test
	@# left running by a DIFFERENT worktree/checkout (different
	@# working_dir label, same fixed name) collides on `docker compose up`.
	@# Detect that case and remove the stale container so this target stays
	@# re-runnable without a manual `docker rm` — never touch it if it
	@# belongs to THIS worktree (that one gets reused as intended).
	@owner="$$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' self-docs-db-test 2>/dev/null || true)"; \
	if [ -n "$$owner" ] && [ "$$owner" != "$(CURDIR)" ]; then \
		echo "Stale self-docs-db-test container from another worktree ($$owner) detected — removing it so this worktree can start its own."; \
		docker rm -f self-docs-db-test >/dev/null 2>&1 || true; \
	fi
	docker compose -f docker-compose.yml -f docker-compose.test.yml up -d db-test
	@echo "Waiting for db-test to become healthy..."
	@for i in $$(seq 1 30); do \
		status=$$(docker inspect -f '{{.State.Health.Status}}' self-docs-db-test 2>/dev/null || echo starting); \
		if [ "$$status" = "healthy" ]; then echo "db-test is healthy"; exit 0; fi; \
		sleep 1; \
	done; \
	echo "db-test did not become healthy in time" >&2; \
	docker compose -f docker-compose.yml -f docker-compose.test.yml logs db-test; \
	exit 1

# Tear down the db-test container (keeps the pgdata_test volume — use
# test-db-reset to wipe data too).
test-db-down:
	docker compose -f docker-compose.yml -f docker-compose.test.yml stop db-test

# Disposable test database: stop db-test and delete its volume, so the next
# `make test-db-up` re-applies db/init/ from scratch on an empty volume.
# Usage: make test-db-reset
test-db-reset:
	docker compose -f docker-compose.yml -f docker-compose.test.yml down -v db-test
	@echo "db-test container and pgdata_test volume removed. Run 'make test' or 'make test-db-up' to recreate."

# Runs the full suite for BOTH packages (ingestion, mcp-server), doc-cli Go suite, plus the
# cross-package e2e test, as the single `make test` entrypoint from repo root.
# Brings up the isolated db-test service first (never the production `db`),
# points every DB-backed suite at it via TEST_POSTGRES_* (see top of this
# file — the SAME variables docker-compose.test.yml reads), and pins
# EMBEDDING_DIM/EMBEDDING_MODEL_NAME to the deployed default
# (BAAI/bge-small-en-v1.5, dim 384 — matching db/init/01_schema.sql's
# vector(384)) so DB-backed tests don't fail on the stale 1024-dim mxbai
# fallback constants baked into the app code.
#
# Two tests are KNOWN-RED and left that way on purpose (user-approved,
# out of scope to fix here — the mxbai-vs-registry embedding-default
# mismatch): mcp-server/tests/test_registry_defaults.py::
# test_retrieval_defaults_match_registry_default and tests/
# test_model_registry.py::test_ingestion_embedder_defaults_match_registry_default.
# Every suite below therefore runs to completion regardless of earlier
# failures (`|| SUITE_FAIL=1`, no suite short-circuits the next), and the
# target still exits non-zero overall so a real regression can't hide behind
# the two expected failures.
test: test-db-up
	@SUITE_FAIL=0; \
	echo "=== doc-cli Go test suite ==="; \
	(cd cli && go test -v ./...) || SUITE_FAIL=1; \
	echo "=== ensuring ingestion/.venv ==="; \
	test -d ingestion/.venv || python3 -m venv ingestion/.venv; \
	ingestion/.venv/bin/pip install -q -U pip; \
	ingestion/.venv/bin/pip install -q -e ingestion; \
	ingestion/.venv/bin/pip install -q pytest pytest-cov; \
	echo "=== ensuring mcp-server/.venv ==="; \
	test -d mcp-server/.venv || python3 -m venv mcp-server/.venv; \
	mcp-server/.venv/bin/pip install -q -U pip; \
	mcp-server/.venv/bin/pip install -q -e mcp-server; \
	mcp-server/.venv/bin/pip install -q pytest pytest-cov pyyaml defusedxml; \
	echo "=== ingestion test suite ==="; \
	(cd ingestion && POSTGRES_HOST=$(TEST_POSTGRES_HOST) POSTGRES_PORT=$(TEST_POSTGRES_PORT) POSTGRES_USER=$(TEST_POSTGRES_USER) POSTGRES_PASSWORD=$(TEST_POSTGRES_PASSWORD) POSTGRES_DB=$(TEST_POSTGRES_DB) EMBEDDING_DIM=384 EMBEDDING_MODEL_NAME=BAAI/bge-small-en-v1.5 ../ingestion/.venv/bin/pytest -q --cov=app --cov-report=term-missing:skip-covered) || SUITE_FAIL=1; \
	echo "=== mcp-server test suite ==="; \
	(cd mcp-server && POSTGRES_HOST=$(TEST_POSTGRES_HOST) POSTGRES_PORT=$(TEST_POSTGRES_PORT) POSTGRES_USER=$(TEST_POSTGRES_USER) POSTGRES_PASSWORD=$(TEST_POSTGRES_PASSWORD) POSTGRES_DB=$(TEST_POSTGRES_DB) EMBEDDING_DIM=384 EMBEDDING_MODEL_NAME=BAAI/bge-small-en-v1.5 ../mcp-server/.venv/bin/pytest -q --cov=app --cov-report=term-missing:skip-covered) || SUITE_FAIL=1; \
	echo "=== e2e (cross-package) test suite ==="; \
	(cd tests && POSTGRES_HOST=$(TEST_POSTGRES_HOST) POSTGRES_PORT=$(TEST_POSTGRES_PORT) POSTGRES_USER=$(TEST_POSTGRES_USER) POSTGRES_PASSWORD=$(TEST_POSTGRES_PASSWORD) POSTGRES_DB=$(TEST_POSTGRES_DB) EMBEDDING_DIM=384 EMBEDDING_MODEL_NAME=BAAI/bge-small-en-v1.5 ../ingestion/.venv/bin/python -m pytest -q) || SUITE_FAIL=1; \
	if [ "$$SUITE_FAIL" -ne 0 ]; then \
		echo "make test: FAILURES present (expected: the 2 documented known-red"; \
		echo "registry-default-mismatch tests — see the comment above this target;"; \
		echo "anything else here is a real regression). DB-backed tests ran against"; \
		echo "the isolated db-test service on $(TEST_POSTGRES_HOST):$(TEST_POSTGRES_PORT)"; \
		echo "(own container, own pgdata_test volume) — the production db/pgdata"; \
		echo "volume was never touched."; \
		exit 1; \
	else \
		echo "make test: all suites green. DB-backed tests ran against the isolated"; \
		echo "db-test service on $(TEST_POSTGRES_HOST):$(TEST_POSTGRES_PORT) (own container,"; \
		echo "own pgdata_test volume) — the production db/pgdata volume was never touched."; \
	fi


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
