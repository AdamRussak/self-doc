.PHONY: up down sync test backup restore

# Bring up the full stack (db + ingestion + mcp-server). Until T2/T3 land their
# Dockerfiles, use `docker compose up -d db` directly to bring up just Postgres.
up:
	docker compose --profile full up -d

down:
	docker compose down

# Trigger a documentation sync via the ingestion service's /sync endpoint.
# Requires SYNC_TOKEN to be set in the environment (see .env).
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
	@mcp-server/.venv/bin/pip install -q pytest
	@echo "=== ingestion test suite ==="
	cd ingestion && ../ingestion/.venv/bin/pytest -q
	@echo "=== mcp-server test suite ==="
	cd mcp-server && ../mcp-server/.venv/bin/pytest -q
	@echo "=== e2e (cross-package) test suite ==="
	cd tests && ../ingestion/.venv/bin/python -m pytest -q
	@echo "make test: all suites green (DB-dependent tests skip cleanly if 'docker compose up -d db' wasn't run first)."

# Dump the docs database to a timestamped custom-format archive under ./backups.
backup:
	mkdir -p backups
	docker compose exec -T db pg_dump -U $${POSTGRES_USER} -d $${POSTGRES_DB} -Fc \
		> backups/docs_$$(date +%Y%m%d_%H%M%S).dump
	@echo "Backup written to backups/docs_<timestamp>.dump"

# Restore from a backup produced by `make backup`.
# Usage: make restore FILE=backups/docs_20260101_030000.dump
restore:
	@if [ -z "$(FILE)" ]; then echo "Usage: make restore FILE=backups/docs_<timestamp>.dump"; exit 1; fi
	cat $(FILE) | docker compose exec -T db pg_restore -U $${POSTGRES_USER} -d $${POSTGRES_DB} --clean --if-exists
	@echo "Restore complete. pg_dump preserves the HNSW index definition but not"
	@echo "its build — run REINDEX INDEX doc_chunks_embedding_idx; inside the db"
	@echo "container to rebuild it (can take a while on a large corpus)."
