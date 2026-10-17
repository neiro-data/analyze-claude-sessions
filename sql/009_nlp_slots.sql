-- Typed slots on the NLP catalog: `acp ask` refuses a match when the
-- question carries a filter (session_id, since, project, model, limit) that
-- the matched row's sql_text has no placeholder for, instead of silently
-- dropping it. See src/analyze_claude_sessions/nlp.py:SlotSpec.
--
-- Idempotent: safe to replay via `acp init-db` on a fresh install (where
-- sql/005_nlp_catalog.sql already creates `slots`) or a previously migrated
-- one. Keep the two files' `slots` values in sync — a fresh install and a
-- migrated install must produce byte-identical `nlp_examples` contents.

ALTER TABLE claude.nlp_examples
    ADD COLUMN IF NOT EXISTS slots jsonb NOT NULL DEFAULT '[]';

-- Drop the duplicate row that shared sql_text with 'how many tokens were
-- spent per model' (also removed from sql/005_nlp_catalog.sql).
DELETE FROM claude.nlp_examples WHERE question = 'what is the total tokens per model';

-- Non-session-scoped rows have no %s placeholders today, so they declare no
-- slots (an empty array, already the column default -- listed for clarity).
UPDATE claude.nlp_examples SET slots = '[]'::jsonb
 WHERE question IN (
    'how many sessions are there',
    'how many projects are there',
    'how many transcripts are there',
    'how many subagent transcripts are there',
    'how many api calls are there',
    'how many tokens were spent per model',
    'total tokens per project',
    'total tokens per day',
    'tokens by subagent vs main',
    'top 10 sessions by total tokens',
    'top 10 projects by tokens',
    'what is the average session duration',
    'what is the overall cache read percentage',
    'what are the longest gaps between calls',
    'which model is used most',
    'how many sessions per project',
    'output tokens per model',
    'daily api call trend',
    'what is the total number of tokens overall'
 );

-- Session-scoped rows each bind exactly one %s to session_id.
UPDATE claude.nlp_examples
   SET slots = '[{"name": "session_id", "type": "session_id"}]'::jsonb
 WHERE question IN (
    'cache report',
    'tokens spent in this session',
    'tokens spent per model in this session',
    'tokens spent per agent in this session',
    'number of calls in this session',
    'number of calls per agent in this session',
    'complete session output ordered by time',
    'session summary',
    'longest gaps in this session'
 );
