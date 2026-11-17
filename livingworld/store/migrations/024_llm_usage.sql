-- 024: AI cost logging (livingworld/perf/llm_usage.py). Every LLM call now fills prompt_tokens,
-- completion_tokens and cost_usd of llm_request; these columns say which lane it ran in, how many prompt
-- tokens the provider served from its cache, whether the tokens were counted locally because the service
-- sent none, and where the cost came from ('reported','priced','free','local','unknown','mixed').
ALTER TABLE llm_request ADD COLUMN lane TEXT;
ALTER TABLE llm_request ADD COLUMN cached_tokens INTEGER;
ALTER TABLE llm_request ADD COLUMN estimated INTEGER NOT NULL DEFAULT 0;
ALTER TABLE llm_request ADD COLUMN cost_source TEXT;
CREATE INDEX llm_request_at ON llm_request(at);

INSERT INTO schema_version(version, note) VALUES (24, 'AI cost logging: lane, cached tokens, cost source');
