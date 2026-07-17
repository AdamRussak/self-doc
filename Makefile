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

test:
	pytest

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
