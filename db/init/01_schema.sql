CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE doc_sources (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,      -- e.g. "nextjs", matches sources.yaml key
    base_url      TEXT NOT NULL,
    last_synced   TIMESTAMPTZ,
    last_status   TEXT                       -- ok | partial | failed
);

CREATE TABLE doc_pages (
    id            SERIAL PRIMARY KEY,
    source_id     INT NOT NULL REFERENCES doc_sources(id) ON DELETE CASCADE,
    url           TEXT NOT NULL UNIQUE,
    content_hash  CHAR(64) NOT NULL,         -- SHA-256 of extracted markdown
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE doc_chunks (
    id            BIGSERIAL PRIMARY KEY,
    page_id       INT NOT NULL REFERENCES doc_pages(id) ON DELETE CASCADE,
    heading_path  TEXT,                      -- "Guide > Routing > Dynamic Routes"
    chunk_index   INT NOT NULL,
    content       TEXT NOT NULL,             -- markdown
    embedding     vector(384) NOT NULL,
    fts           tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
);

CREATE INDEX doc_chunks_embedding_idx ON doc_chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX doc_chunks_fts_idx ON doc_chunks USING gin (fts);
CREATE INDEX doc_chunks_page_idx ON doc_chunks (page_id);
