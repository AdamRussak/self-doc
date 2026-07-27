-- GENERATED from db/init/01_schema.sql.template by scripts/configure_model.py.
-- Do not edit by hand — run `make configure MODEL=<name>` to change the vector
-- dimension. Rendered for embedding dimension 384.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE doc_sources (
    id                 SERIAL PRIMARY KEY,
    name               TEXT NOT NULL UNIQUE,      -- e.g. "nextjs", matches sources.yaml key
    base_url           TEXT NOT NULL,
    last_synced        TIMESTAMPTZ,
    last_status        TEXT,                      -- ok | partial | failed
    llms_txt           TEXT NOT NULL DEFAULT 'auto'
                            CHECK (llms_txt IN ('auto', 'off', 'only')),
    llms_etag          TEXT,
    llms_last_modified TEXT,
    -- 'crawl' (default): base_url is a live site crawled by the ingestion
    -- pipeline. 'upload': base_url is an 'upload://{name}/{rel_path}'
    -- sentinel — content came from an uploaded file (Markdown/HTML/PDF/zip)
    -- rather than a crawl; see db/init/04_upload_sources.sql.
    source_type        TEXT NOT NULL DEFAULT 'crawl'
                            CHECK (source_type IN ('crawl', 'upload'))
);

CREATE TABLE doc_pages (
    id            SERIAL PRIMARY KEY,
    source_id     INT NOT NULL REFERENCES doc_sources(id) ON DELETE CASCADE,
    url           TEXT NOT NULL UNIQUE,
    content_hash  CHAR(64) NOT NULL,         -- SHA-256 of extracted markdown
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    etag          TEXT,
    last_modified TEXT
);

CREATE TABLE doc_chunks (
    id            BIGSERIAL PRIMARY KEY,
    page_id       INT NOT NULL REFERENCES doc_pages(id) ON DELETE CASCADE,
    heading_path  TEXT,                      -- "Guide > Routing > Dynamic Routes"
    chunk_index   INT NOT NULL,
    content       TEXT NOT NULL,             -- markdown
    embedding     vector(384) NOT NULL,
    fts_config    regconfig NOT NULL DEFAULT 'english',
    fts           tsvector GENERATED ALWAYS AS (to_tsvector(fts_config, content)) STORED
);

CREATE INDEX doc_chunks_embedding_idx ON doc_chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX doc_chunks_fts_idx ON doc_chunks USING gin (fts);
CREATE INDEX doc_chunks_page_idx ON doc_chunks (page_id);
