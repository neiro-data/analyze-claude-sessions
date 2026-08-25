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
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (question)
);

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

INSERT INTO claude.nlp_examples (question, sql_text, description, tags)
VALUES
(
    'how many sessions are there',
    'SELECT count(*) AS session_count FROM claude.sessions',
    'Total number of ingested sessions.',
    ARRAY['count', 'sessions']
),
(
    'how many projects are there',
    'SELECT count(*) AS project_count FROM claude.projects',
    'Total number of distinct projects.',
    ARRAY['count', 'projects']
),
(
    'how many transcripts are there',
    'SELECT count(*) AS transcript_count FROM claude.transcripts',
    'Total number of transcripts (main + subagent).',
    ARRAY['count', 'transcripts']
),
(
    'how many subagent transcripts are there',
    'SELECT count(*) AS subagent_transcript_count FROM claude.transcripts WHERE kind = ''subagent''',
    'Total number of subagent transcripts.',
    ARRAY['count', 'transcripts', 'subagent']
),
(
    'how many api calls are there',
    'SELECT count(*) AS api_call_count FROM claude.api_calls',
    'Total number of API calls recorded.',
    ARRAY['count', 'api_calls']
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
    ARRAY['tokens', 'model', 'group by']
),
(
    'what is the total tokens per model',
    'SELECT model,
            sum(total_input_tokens)                       AS total_input_tokens,
            sum(output_tokens)                             AS output_tokens,
            sum(total_input_tokens) + sum(output_tokens)   AS total_tokens
       FROM claude.api_calls
      GROUP BY model
      ORDER BY total_tokens DESC',
    'Total tokens (input incl. cache reads/writes, plus output) grouped by model.',
    ARRAY['tokens', 'model', 'group by']
),
(
    'total tokens per project',
    'SELECT project_dir, total_input_tokens, output_tokens
       FROM claude.v_project_token_summary
      ORDER BY total_input_tokens DESC NULLS LAST',
    'Billed input + output tokens per project.',
    ARRAY['tokens', 'project']
),
(
    'total tokens per day',
    'SELECT usage_date, total_input_tokens, output_tokens, api_calls
       FROM claude.v_daily_token_usage
      ORDER BY usage_date',
    'Daily token usage trend.',
    ARRAY['tokens', 'daily', 'trend']
),
(
    'tokens by subagent vs main',
    'SELECT kind, api_calls, total_input_tokens, output_tokens, pct_of_input_tokens
       FROM claude.v_subagent_share
      ORDER BY kind',
    'Split of token usage between main transcripts and subagents.',
    ARRAY['tokens', 'subagent', 'main']
),
(
    'top 10 sessions by total tokens',
    'SELECT session_id, project_dir, total_input_tokens, output_tokens
       FROM claude.v_session_token_summary
      ORDER BY total_input_tokens DESC NULLS LAST
      LIMIT 10',
    'The ten sessions with the highest billed token usage.',
    ARRAY['tokens', 'sessions', 'top']
),
(
    'top 10 projects by tokens',
    'SELECT project_dir, total_input_tokens, output_tokens
       FROM claude.v_project_token_summary
      ORDER BY total_input_tokens DESC NULLS LAST
      LIMIT 10',
    'The ten projects with the highest billed token usage.',
    ARRAY['tokens', 'projects', 'top']
),
(
    'what is the average session duration',
    'SELECT round(avg(duration_seconds)::numeric, 1) AS avg_duration_seconds
       FROM claude.sessions',
    'Average session duration in seconds.',
    ARRAY['duration', 'average', 'sessions']
),
(
    'what is the overall cache read percentage',
    'SELECT round(coalesce(sum(cache_read_tokens), 0)::numeric
                  / NULLIF(sum(total_input_tokens), 0) * 100, 2) AS cache_read_pct
       FROM claude.api_calls',
    'Overall percentage of billed input tokens served from cache.',
    ARRAY['cache', 'percentage']
),
(
    'what are the longest gaps between calls',
    'SELECT session_id, project_dir, kind, call_index, called_at, delta_seconds
       FROM claude.v_long_gaps
      ORDER BY delta_seconds DESC
      LIMIT 20',
    'Longest pauses between consecutive API calls within a transcript.',
    ARRAY['gaps', 'pauses', 'duration']
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
    ARRAY['model', 'most used']
),
(
    'how many sessions per project',
    'SELECT project_dir, sessions
       FROM claude.v_project_token_summary
      ORDER BY sessions DESC',
    'Number of sessions per project.',
    ARRAY['sessions', 'project', 'count']
),
(
    'output tokens per model',
    'SELECT model, sum(output_tokens) AS output_tokens
       FROM claude.api_calls
      GROUP BY model
      ORDER BY output_tokens DESC',
    'Output tokens grouped by model.',
    ARRAY['tokens', 'model', 'output']
),
(
    'daily api call trend',
    'SELECT usage_date, api_calls, sessions
       FROM claude.v_daily_token_usage
      ORDER BY usage_date',
    'Number of API calls and active sessions per day.',
    ARRAY['api_calls', 'daily', 'trend']
),
(
    'what is the total number of tokens overall',
    'SELECT coalesce(sum(total_input_tokens), 0) AS total_input_tokens,
            coalesce(sum(output_tokens), 0)      AS output_tokens
       FROM claude.api_calls',
    'Grand total of billed input and output tokens across all calls.',
    ARRAY['tokens', 'total', 'overall']
),
(
    'cache report',
    'SELECT coalesce(agent,''main'') AS agent,
            count(*)                                        AS calls,
            sum(cache_write_tokens)                          AS cache_write_tokens,
            sum(cache_read_tokens)                           AS cache_read_tokens,
            sum(output_tokens)                               AS output_tokens,
            round(sum(cache_read_tokens)::numeric
                  / NULLIF(sum(total_input_tokens), 0) * 100, 1) AS cache_read_pct
       FROM claude.api_calls
      WHERE session_id = %s
      GROUP BY coalesce(agent, ''main'')
      ORDER BY cache_read_tokens DESC',
    'Per-agent cache write/read/output and cache-read % for one session.',
    ARRAY['session_scoped', 'cache', 'session']
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
    ARRAY['session_scoped', 'tokens', 'session']
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
    ARRAY['session_scoped', 'tokens', 'model', 'session']
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
    ARRAY['session_scoped', 'tokens', 'agent', 'session']
),
(
    'number of calls in this session',
    'SELECT count(*) AS api_calls FROM claude.api_calls WHERE session_id = %s',
    'Total number of API calls in one session.',
    ARRAY['session_scoped', 'count', 'session']
),
(
    'number of calls per agent in this session',
    'SELECT coalesce(agent, ''main'') AS agent, count(*) AS api_calls
       FROM claude.api_calls
      WHERE session_id = %s
      GROUP BY coalesce(agent, ''main'')
      ORDER BY api_calls DESC',
    'Number of API calls per agent within one session.',
    ARRAY['session_scoped', 'count', 'agent', 'session']
),
(
    'complete session output ordered by time',
    'SELECT call_index, called_at, coalesce(agent, ''main'') AS agent, agent_type,
            depth, model, total_input_tokens, output_tokens, delta_seconds, description
       FROM claude.api_calls
      WHERE session_id = %s
      ORDER BY called_at, call_index',
    'Full chronological call log for one session.',
    ARRAY['session_scoped', 'session', 'timeline']
),
(
    'session summary',
    'SELECT * FROM claude.v_session_token_summary WHERE session_id = %s',
    'Token usage summary for one session.',
    ARRAY['session_scoped', 'session', 'summary']
),
(
    'longest gaps in this session',
    'SELECT call_index, called_at, coalesce(agent, ''main'') AS agent, delta_seconds
       FROM claude.api_calls
      WHERE session_id = %s
      ORDER BY delta_seconds DESC
      LIMIT 20',
    'The twenty longest pauses between consecutive calls within one session.',
    ARRAY['session_scoped', 'session', 'gaps']
)
ON CONFLICT (question)
DO UPDATE SET sql_text    = EXCLUDED.sql_text,
              description = EXCLUDED.description,
              tags        = EXCLUDED.tags;
