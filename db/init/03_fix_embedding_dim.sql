-- Idempotent migration script to update canonical URLs, sitemaps,
-- and include_prefixes for doc sources that failed initial ingestion.

-- 1. anthropic-api: Update to canonical platform URL & sitemap
UPDATE doc_sources 
SET base_url = 'https://platform.claude.com/docs/en/home',
    sitemap = 'https://platform.claude.com/sitemap.xml',
    include_prefixes = ARRAY['/docs/en/']
WHERE name = 'anthropic-api';

-- 2. cloudflare-developers: Fix sitemap redirect target
UPDATE doc_sources
SET sitemap = 'https://developers.cloudflare.com/sitemap-index.xml'
WHERE name = 'cloudflare-developers';

-- 3. elevenlabs-api: Fix include_prefixes to match site structure
UPDATE doc_sources
SET include_prefixes = ARRAY['/docs/api-reference/', '/docs/guides/', '/docs/overview']
WHERE name = 'elevenlabs-api';

-- 4. gemini-api: Fix base_url and include_prefixes for Gemini API docs
UPDATE doc_sources
SET base_url = 'https://ai.google.dev/gemini-api/docs',
    sitemap = 'https://ai.google.dev/sitemap.xml',
    include_prefixes = ARRAY['/gemini-api/docs']
WHERE name = 'gemini-api';

-- 5. google-pagespeed-api: Switch to BFS (small docs set)
UPDATE doc_sources
SET base_url = 'https://developers.google.com/speed/docs/insights/v5/about',
    sitemap = NULL,
    include_prefixes = ARRAY['/speed/docs/insights/']
WHERE name = 'google-pagespeed-api';

-- 6. google-maps-platform: Drop broken sitemap
UPDATE doc_sources
SET sitemap = NULL,
    include_prefixes = ARRAY['/maps/documentation/']
WHERE name = 'google-maps-platform';

-- 7. openai-api: Migrate to new developer docs domain
UPDATE doc_sources
SET base_url = 'https://developers.openai.com/docs/overview',
    sitemap = 'https://developers.openai.com/sitemap.xml',
    include_prefixes = ARRAY['/docs/', '/api/']
WHERE name = 'openai-api';

-- 8. pgvector-readme: Disable llms.txt, use BFS
UPDATE doc_sources
SET llms_txt = 'off',
    sitemap = NULL,
    max_pages = 3,
    include_prefixes = ARRAY['/pgvector/pgvector']
WHERE name = 'pgvector-readme';

-- 9. unsplash-api: Fix base_url, drop sitemap, disable llms
UPDATE doc_sources
SET base_url = 'https://unsplash.com/documentation',
    sitemap = NULL,
    include_prefixes = ARRAY['/documentation'],
    rate_limit_rps = 0.5,
    llms_txt = 'off'
WHERE name = 'unsplash-api';

-- 10. wikidata-api: Fix to actual documentation page
UPDATE doc_sources
SET base_url = 'https://www.wikidata.org/wiki/Wikidata:REST_API',
    sitemap = NULL,
    include_prefixes = ARRAY['/wiki/Wikidata:REST_API', '/wiki/Wikidata:Data_access'],
    llms_txt = 'off'
WHERE name = 'wikidata-api';
