-- Offline NLP / RAG layer: a curated question -> SQL catalog plus a schema
-- navigation view, both idempotent so `acp init-db` can replay this safely.
--
-- claude.nlp_examples is the retrieval corpus for `acp ask`: local sentence
-- embeddings are computed over `question` at query time, the best match's
-- `sql_text` is executed read-only. claude.v_schema_catalog is a
-- human/agent-readable index of queryable objects and their columns.

CREATE TABLE IF NOT EXISTS claude.nlp_examples (
    example_id  bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    question    text        NOT NULL,
    sql_text    text        NOT NULL,
    description text,
    tags        text[]      NOT NULL DEFAULT '{}',
    -- Typed filters this row's sql_text can bind, e.g. '[{"name": "session_id",
    -- "type": "session_id"}]'. One entry per `%s` placeholder, in bind order.
    -- See src/analyze_claude_projects/nlp.py:SlotSpec. A fresh install (this
    -- file) and a migrated one (sql/009_nlp_slots.sql) must agree exactly.
    slots       jsonb       NOT NULL DEFAULT '[]',
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (question)
);

-- All sql/*.sql files re-run on every `acp init-db` (see apply_migrations),
-- in filename order. On an install migrated before `slots` existed, this
-- file's own INSERT below (which lists `slots`) would fail unless the
-- column is guaranteed present *before* it runs -- it cannot rely on
-- sql/009_nlp_slots.sql, which sorts after this file and would already be
-- too late. Idempotent on a fresh install too, where the CREATE TABLE above
-- already includes the column.
ALTER TABLE claude.nlp_examples ADD COLUMN IF NOT EXISTS slots jsonb NOT NULL DEFAULT '[]';

CREATE OR REPLACE VIEW claude.v_schema_catalog AS
SELECT
    c.table_name                                            AS object_name,
    CASE t.table_type
        WHEN 'VIEW' THEN 'view'
        ELSE 'table'
    END                                                      AS object_type,
    c.column_name,
    c.data_type,
    col_description(
        (quote_ident(c.table_schema) || '.' || quote_ident(c.table_name))::regclass::oid,
        c.ordinal_position
    )                                                        AS column_comment
FROM information_schema.columns c
JOIN information_schema.tables t
  ON t.table_schema = c.table_schema AND t.table_name = c.table_name
WHERE c.table_schema = 'claude'
ORDER BY c.table_name, c.ordinal_position;

-- Shorthand for the single-session_id-placeholder shape shared by every
-- session-scoped row below (all current session-scoped rows bind exactly
-- one %s).
INSERT INTO claude.nlp_examples (question, sql_text, description, tags, slots)
VALUES
(
    'how many sessions are there',
    'SELECT count(*) AS session_count FROM claude.sessions',
    'Total number of ingested sessions.',
    ARRAY['count', 'sessions'],
    '[]'::jsonb
),
(
    'how many projects are there',
    'SELECT count(*) AS project_count FROM claude.projects',
    'Total number of distinct projects.',
    ARRAY['count', 'projects'],
    '[]'::jsonb
),
(
    'how many transcripts are there',
    'SELECT count(*) AS transcript_count FROM claude.transcripts',
    'Total number of transcripts (main + subagent).',
    ARRAY['count', 'transcripts'],
    '[]'::jsonb
),
(
    'how many subagent transcripts are there',
    'SELECT count(*) AS subagent_transcript_count FROM claude.transcripts WHERE kind = ''subagent''',
    'Total number of subagent transcripts.',
    ARRAY['count', 'transcripts', 'subagent'],
    '[]'::jsonb
),
(
    'how many api calls are there',
    'SELECT count(*) AS api_call_count FROM claude.api_calls',
    'Total number of API calls recorded.',
    ARRAY['count', 'api_calls'],
    '[]'::jsonb
),
(
    'how many tokens were spent per model',
    'SELECT model,
            sum(total_input_tokens)                       AS total_input_tokens,
            sum(output_tokens)                             AS output_tokens,
            sum(total_input_tokens) + sum(output_tokens)   AS total_tokens
       FROM claude.api_calls
      GROUP BY model
      ORDER BY total_tokens DESC',
    'Total tokens (input incl. cache reads/writes, plus output) grouped by model.',
    ARRAY['tokens', 'model', 'group by'],
    '[]'::jsonb
),
(
    'total tokens per project',
    'SELECT project_dir, total_input_tokens, output_tokens
       FROM claude.v_project_token_summary
      ORDER BY total_input_tokens DESC NULLS LAST',
    'Billed input + output tokens per project.',
    ARRAY['tokens', 'project'],
    '[]'::jsonb
),
(
    'total tokens per day',
    'SELECT usage_date, total_input_tokens, output_tokens, api_calls
       FROM claude.v_daily_token_usage
      ORDER BY usage_date',
    'Daily token usage trend.',
    ARRAY['tokens', 'daily', 'trend'],
    '[]'::jsonb
),
(
    'tokens by subagent vs main',
    'SELECT kind, api_calls, total_input_tokens, output_tokens, pct_of_input_tokens
       FROM claude.v_subagent_share
      ORDER BY kind',
    'Split of token usage between main transcripts and subagents.',
    ARRAY['tokens', 'subagent', 'main'],
    '[]'::jsonb
),
(
    'top 10 sessions by total tokens',
    'SELECT session_id, project_dir, total_input_tokens, output_tokens
       FROM claude.v_session_token_summary
      ORDER BY total_input_tokens DESC NULLS LAST
      LIMIT 10',
    'The ten sessions with the highest billed token usage.',
    ARRAY['tokens', 'sessions', 'top'],
    '[]'::jsonb
),
(
    'top 10 projects by tokens',
    'SELECT project_dir, total_input_tokens, output_tokens
       FROM claude.v_project_token_summary
      ORDER BY total_input_tokens DESC NULLS LAST
      LIMIT 10',
    'The ten projects with the highest billed token usage.',
    ARRAY['tokens', 'projects', 'top'],
    '[]'::jsonb
),
(
    'what is the average session duration',
    'SELECT round(avg(duration_seconds)::numeric, 1) AS avg_duration_seconds
       FROM claude.sessions',
    'Average session duration in seconds.',
    ARRAY['duration', 'average', 'sessions'],
    '[]'::jsonb
),
(
    'what is the overall cache read percentage',
    'SELECT round(coalesce(sum(cache_read_tokens), 0)::numeric
                  / NULLIF(sum(total_input_tokens), 0) * 100, 2) AS cache_read_pct
       FROM claude.api_calls',
    'Overall percentage of billed input tokens served from cache.',
    ARRAY['cache', 'percentage'],
    '[]'::jsonb
),
(
    'what are the longest gaps between calls',
    'SELECT session_id, project_dir, kind, call_index, called_at, delta_seconds
       FROM claude.v_long_gaps
      ORDER BY delta_seconds DESC
      LIMIT 20',
    'Longest pauses between consecutive API calls within a transcript.',
    ARRAY['gaps', 'pauses', 'duration'],
    '[]'::jsonb
),
(
    'which model is used most',
    'SELECT model, count(*) AS api_calls
       FROM claude.api_calls
      WHERE model IS NOT NULL
      GROUP BY model
      ORDER BY api_calls DESC
      LIMIT 1',
    'The most-used model by number of API calls.',
    ARRAY['model', 'most used'],
    '[]'::jsonb
),
(
    'how many sessions per project',
    'SELECT project_dir, sessions
       FROM claude.v_project_token_summary
      ORDER BY sessions DESC',
    'Number of sessions per project.',
    ARRAY['sessions', 'project', 'count'],
    '[]'::jsonb
),
(
    'output tokens per model',
    'SELECT model, sum(output_tokens) AS output_tokens
       FROM claude.api_calls
      GROUP BY model
      ORDER BY output_tokens DESC',
    'Output tokens grouped by model.',
    ARRAY['tokens', 'model', 'output'],
    '[]'::jsonb
),
(
    'daily api call trend',
    'SELECT usage_date, api_calls, sessions
       FROM claude.v_daily_token_usage
      ORDER BY usage_date',
    'Number of API calls and active sessions per day.',
    ARRAY['api_calls', 'daily', 'trend'],
    '[]'::jsonb
),
(
    'what is the total number of tokens overall',
    'SELECT coalesce(sum(total_input_tokens), 0) AS total_input_tokens,
            coalesce(sum(output_tokens), 0)      AS output_tokens
       FROM claude.api_calls',
    'Grand total of billed input and output tokens across all calls.',
    ARRAY['tokens', 'total', 'overall'],
    '[]'::jsonb
),
(
    'cache report',
    'SELECT coalesce(agent_type, ''main'')                  AS agent,
            count(*)                                        AS calls,
            sum(cache_write_tokens)                          AS cache_write_tokens,
            sum(cache_read_tokens)                           AS cache_read_tokens,
            round(sum(cache_read_tokens)::numeric
                  / NULLIF(sum(cache_write_tokens), 0), 1)   AS reuse,
            sum(output_tokens)                               AS output_tokens,
            round(sum(cache_read_tokens)::numeric
                  / NULLIF(sum(total_input_tokens), 0) * 100, 1) AS cache_read_pct
       FROM claude.api_calls
      WHERE session_id = %s
      GROUP BY coalesce(agent_type, ''main'')
      ORDER BY cache_read_tokens DESC',
    'Per-agent (by name) cache write/read/reuse/output and cache-read % for one session.',
    ARRAY['session_scoped', 'cache', 'session'],
    '[{"name": "session_id", "type": "session_id"}]'::jsonb
),
(
    'tokens spent in this session',
    'SELECT coalesce(sum(total_input_tokens), 0) AS total_input_tokens,
            coalesce(sum(cache_write_tokens), 0) AS cache_write_tokens,
            coalesce(sum(cache_read_tokens), 0)  AS cache_read_tokens,
            coalesce(sum(output_tokens), 0)      AS output_tokens
       FROM claude.api_calls
      WHERE session_id = %s',
    'Total input/cache/output tokens spent in one session.',
    ARRAY['session_scoped', 'tokens', 'session'],
    '[{"name": "session_id", "type": "session_id"}]'::jsonb
),
(
    'tokens spent per model in this session',
    'SELECT model,
            sum(total_input_tokens)                       AS total_input_tokens,
            sum(output_tokens)                             AS output_tokens,
            sum(total_input_tokens) + sum(output_tokens)   AS total_tokens
       FROM claude.api_calls
      WHERE session_id = %s
      GROUP BY model
      ORDER BY total_tokens DESC',
    'Tokens spent per model within one session.',
    ARRAY['session_scoped', 'tokens', 'model', 'session'],
    '[{"name": "session_id", "type": "session_id"}]'::jsonb
),
(
    'tokens spent per agent in this session',
    'SELECT coalesce(agent, ''main'')       AS agent,
            coalesce(agent_type, '''')      AS agent_type,
            sum(total_input_tokens)         AS total_input_tokens,
            sum(output_tokens)              AS output_tokens,
            sum(total_input_tokens) + sum(output_tokens) AS total_tokens
       FROM claude.api_calls
      WHERE session_id = %s
      GROUP BY coalesce(agent, ''main''), coalesce(agent_type, '''')
      ORDER BY total_tokens DESC',
    'Tokens spent per agent within one session.',
    ARRAY['session_scoped', 'tokens', 'agent', 'session'],
    '[{"name": "session_id", "type": "session_id"}]'::jsonb
),
(
    'number of calls in this session',
    'SELECT count(*) AS api_calls FROM claude.api_calls WHERE session_id = %s',
    'Total number of API calls in one session.',
    ARRAY['session_scoped', 'count', 'session'],
    '[{"name": "session_id", "type": "session_id"}]'::jsonb
),
(
    'number of calls per agent in this session',
    'SELECT coalesce(agent, ''main'') AS agent, count(*) AS api_calls
       FROM claude.api_calls
      WHERE session_id = %s
      GROUP BY coalesce(agent, ''main'')
      ORDER BY api_calls DESC',
    'Number of API calls per agent within one session.',
    ARRAY['session_scoped', 'count', 'agent', 'session'],
    '[{"name": "session_id", "type": "session_id"}]'::jsonb
),
(
    'complete session output ordered by time',
    'SELECT call_index, called_at, coalesce(agent, ''main'') AS agent, agent_type,
            depth, model, total_input_tokens, output_tokens, delta_seconds, description
       FROM claude.api_calls
      WHERE session_id = %s
      ORDER BY called_at, call_index',
    'Full chronological call log for one session.',
    ARRAY['session_scoped', 'session', 'timeline'],
    '[{"name": "session_id", "type": "session_id"}]'::jsonb
),
(
    'session summary',
    'SELECT * FROM claude.v_session_token_summary WHERE session_id = %s',
    'Token usage summary for one session.',
    ARRAY['session_scoped', 'session', 'summary'],
    '[{"name": "session_id", "type": "session_id"}]'::jsonb
),
(
    'longest gaps in this session',
    'SELECT call_index, called_at, coalesce(agent, ''main'') AS agent, delta_seconds
       FROM claude.api_calls
      WHERE session_id = %s
      ORDER BY delta_seconds DESC
      LIMIT 20',
    'The twenty longest pauses between consecutive calls within one session.',
    ARRAY['session_scoped', 'session', 'gaps'],
    '[{"name": "session_id", "type": "session_id"}]'::jsonb
)
ON CONFLICT (question)
DO UPDATE SET sql_text    = EXCLUDED.sql_text,
              description = EXCLUDED.description,
              tags        = EXCLUDED.tags,
              slots       = EXCLUDED.slots;
