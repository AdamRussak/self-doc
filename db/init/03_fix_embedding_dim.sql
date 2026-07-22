-- Idempotent migration script to align vector column dimensions to 1024
-- and update canonical URLs and include_prefixes for redirected/failed doc sources.

-- 1. Check current dimension of doc_chunks.embedding and update if needed
DO $$
DECLARE
    current_dim int;
BEGIN
    SELECT atttypmod INTO current_dim
    FROM pg_attribute
    WHERE attrelid = 'doc_chunks'::regclass AND attname = 'embedding';

    -- If dimension is 384, truncate chunks/pages so they can be cleanly re-embedded with 1024-dim vectors
    IF current_dim IS NOT NULL AND current_dim = 384 THEN
        TRUNCATE TABLE doc_chunks CASCADE;
        TRUNCATE TABLE doc_pages CASCADE;
        ALTER TABLE doc_chunks ALTER COLUMN embedding TYPE vector(1024);
        RAISE NOTICE 'Updated doc_chunks.embedding column from vector(384) to vector(1024)';
    END IF;
END;
$$;

-- 2. Update canonical URLs and include_prefixes for doc_sources
UPDATE doc_sources 
SET base_url = 'https://platform.claude.com/docs/en/home',
    sitemap = 'https://platform.claude.com/sitemap.xml'
WHERE name = 'anthropic-api';

UPDATE doc_sources 
SET base_url = 'https://developers.openai.com/api/docs',
    sitemap = 'https://developers.openai.com/sitemap.xml'
WHERE name = 'openai-api';

UPDATE doc_sources 
SET base_url = 'https://ai.google.dev/gemini-api/docs',
    sitemap = 'https://ai.google.dev/sitemap.xml',
    include_prefixes = ARRAY['/gemini-api/docs']
WHERE name = 'gemini-api';

UPDATE doc_sources 
SET sitemap = NULL,
    include_prefixes = ARRAY['/maps/documentation/']
WHERE name = 'google-maps-platform';

UPDATE doc_sources 
SET base_url = 'https://www.wikidata.org/wiki/Wikidata:REST_API',
    sitemap = NULL,
    include_prefixes = ARRAY['/wiki/Wikidata:REST_API']
WHERE name = 'wikidata-api';

UPDATE doc_sources 
SET base_url = 'https://developers.google.com/search/docs',
    sitemap = 'https://developers.google.com/sitemap.xml'
WHERE name = 'google-search-console-api';

UPDATE doc_sources 
SET rate_limit_rps = 0.2
WHERE name = 'unsplash-api';

UPDATE doc_sources 
SET base_url = 'https://elevenlabs.io/docs/overview/intro'
WHERE name = 'elevenlabs-api';

UPDATE doc_sources 
SET base_url = 'https://developers.google.com/speed/docs/insights/v5/about'
WHERE name = 'google-pagespeed-api';
