-- Paraphrases for the NLP catalog (hybrid retrieval, see nlp.py:CatalogRetriever).
--
-- Each row scores as the max over its question, these paraphrases and its
-- description, so they widen what a row matches without adding catalog rows.
-- Runs after sql/005, 009 and 018 (files replay in name order on every `acp
-- init-db`); the UPDATEs are idempotent. Keep eval questions out of this file:
-- tests/test_ask_eval.py fails if a test.yaml question appears here verbatim.

ALTER TABLE claude.nlp_examples
    ADD COLUMN IF NOT EXISTS paraphrases text[] NOT NULL DEFAULT '{}';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['total sessions recorded', 'size of the session table', 'number of claude code sessions', 'sessions overall count']
 WHERE question = 'how many sessions are there';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['distinct project count', 'number of projects tracked', 'projects overall count', 'how many repos have sessions']
 WHERE question = 'how many projects are there';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['total transcript files', 'number of jsonl transcripts', 'transcripts overall count']
 WHERE question = 'how many transcripts are there';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['subagent transcript total', 'number of sidechain transcripts', 'how many agent transcripts ran as subagents']
 WHERE question = 'how many subagent transcripts are there';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['total api calls recorded', 'number of model calls', 'api calls overall count', 'size of the api call table']
 WHERE question = 'how many api calls are there';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['token usage grouped by model', 'tokens consumed by each model', 'per model token totals', 'model by model token breakdown']
 WHERE question = 'how many tokens were spent per model';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['token consumption by project', 'project by project token totals', 'tokens consumed in each project', 'per project token usage']
 WHERE question = 'total tokens per project';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['token consumption by day', 'per day token totals', 'tokens consumed each day', 'daily token totals']
 WHERE question = 'total tokens per day';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['subagent compared with main agent tokens', 'tokens from sidechains versus main thread', 'main agent and subagent token comparison', 'how tokens divide between subagents and main']
 WHERE question = 'tokens by subagent vs main';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['most token heavy sessions', 'highest token sessions ranking', 'sessions ranked by token consumption', 'largest sessions by tokens']
 WHERE question = 'top 10 sessions by total tokens';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['most token heavy projects', 'projects ranked by token consumption', 'largest projects by token usage', 'highest token projects ranking']
 WHERE question = 'top 10 projects by tokens';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['mean session runtime', 'average time a session lasts', 'typical session duration', 'average length of a session in time']
 WHERE question = 'what is the average session duration';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['share of input served from cache', 'cache hit share overall', 'percentage of input tokens read from cache', 'overall prompt cache hit rate']
 WHERE question = 'what is the overall cache read percentage';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['largest pauses separating api calls', 'longest idle time between requests', 'largest delays between calls', 'longest stretches without a call']
 WHERE question = 'what are the longest gaps between calls';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['most frequently used model', 'model with the highest call count', 'top model by number of calls', 'most popular model']
 WHERE question = 'which model is used most';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['session count by project', 'sessions grouped by project', 'per project session totals', 'number of sessions in every project']
 WHERE question = 'how many sessions per project';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['generated tokens by model', 'output token totals per model', 'per model output usage', 'completion tokens grouped by model']
 WHERE question = 'output tokens per model';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['api calls over time by day', 'calls per day trend', 'day by day number of api calls', 'api call volume each day']
 WHERE question = 'daily api call trend';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['tokens summed over everything', 'lifetime token count', 'whole history token count', 'sum of all tokens']
 WHERE question = 'what is the total number of tokens overall';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['cache usage report', 'cache write read reuse summary', 'prompt cache statistics', 'report on cache efficiency']
 WHERE question = 'cache report';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['token total for the current session', 'how many tokens did this session use', 'this session token usage', 'session token consumption']
 WHERE question = 'tokens spent in this session';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['model token breakdown for this session', 'this session tokens by model', 'per model usage within the session', 'which models used tokens in this session']
 WHERE question = 'tokens spent per model in this session';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['agent token breakdown for this session', 'this session tokens by agent', 'per agent usage within the session', 'which agents used tokens in this session']
 WHERE question = 'tokens spent per agent in this session';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['api call count for this session', 'how many calls did this session make', 'this session request total', 'session call count']
 WHERE question = 'number of calls in this session';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['calls by agent in this session', 'this session api calls grouped by agent', 'per agent call totals for the session', 'which agents made calls in this session']
 WHERE question = 'number of calls per agent in this session';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['full session timeline', 'session events in chronological order', 'chronological listing of the session', 'every call of the session by time']
 WHERE question = 'complete session output ordered by time';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['session overview', 'summary of this session', 'session at a glance', 'high level session recap']
 WHERE question = 'session summary';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['biggest pauses within this session', 'longest idle periods in the session', 'largest delays between calls in this session', 'this session slowest stretches between calls']
 WHERE question = 'longest gaps in this session';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['overall dollar cost', 'grand total cost in usd', 'lifetime spend', 'sum of all costs in dollars']
 WHERE question = 'how much money did I spend in total';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['model by model dollar cost', 'spend grouped by model', 'per model cost totals', 'how much each model has cost']
 WHERE question = 'what is the cost per model';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['project by project dollar cost', 'spend grouped by project', 'per project cost totals', 'how much each project has cost']
 WHERE question = 'what is the cost per project';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['day by day dollar cost', 'spend grouped by day', 'per day cost totals', 'daily spending']
 WHERE question = 'what is the cost per day';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['cost of the current session', 'dollar price of this session', 'this session spend', 'session cost in usd']
 WHERE question = 'how much did this session cost';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['costliest sessions ranking', 'sessions ranked by dollar cost', 'highest spend sessions', 'most money consuming sessions']
 WHERE question = 'top 10 most expensive sessions';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['api calls without pricing', 'calls lacking a cost', 'unpriced api calls', 'calls where the cost is null']
 WHERE question = 'which calls have no price';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['rarest model by call count', 'model with the lowest call count', 'least frequently used model', 'model used the fewest times']
 WHERE question = 'which model is used least';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['share of input written to cache', 'cache creation share overall', 'percentage of input tokens written to cache', 'overall prompt cache write rate']
 WHERE question = 'what is the overall cache write percentage';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['mean tokens in a session', 'tokens per session on average overall', 'typical token usage of a session', 'average session token total']
 WHERE question = 'what is the average number of tokens per session';

UPDATE claude.nlp_examples SET paraphrases = ARRAY['sessions that have a main transcript count', 'number of sessions with a main thread file', 'main transcript session total', 'count of sessions with their own transcript']
 WHERE question = 'how many main transcript sessions are there';
