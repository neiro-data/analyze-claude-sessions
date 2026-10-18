-- Optional filters, cost rows and opposite-intent rows for the NLP catalog.
--
-- Runs after sql/005 and sql/009 (files replay in name order on every `acp
-- init-db`), so the INSERT ... ON CONFLICT below overrides those rows' sql_text
-- and slots; do not edit those rows in 005/009.
--
-- Slot contract (see src/analyze_claude_sessions/nlp.py:SlotSpec): one entry per
-- `%s`, in bind order. An optional filter such as
--   (%s::date IS NULL OR c.called_at >= %s::date)
-- declares its slot twice; a missing value binds the slot's default (NULL =
-- no filter). Every row that declares `since` also declares `until` (exclusive).
-- cost_usd is NULL, never 0, for unpriced calls, so cost rows also report
-- `unpriced_calls`.

INSERT INTO claude.nlp_examples (question, sql_text, description, tags, slots)
VALUES
(
    'how many tokens were spent per model',
    'SELECT c.model,
       sum(c.total_input_tokens)                        AS total_input_tokens,
       sum(c.output_tokens)                             AS output_tokens,
       sum(c.total_input_tokens) + sum(c.output_tokens) AS total_tokens
  FROM claude.api_calls c
       JOIN claude.sessions s ON s.session_id = c.session_id
       JOIN claude.projects p ON p.project_id = s.project_id
 WHERE (%s::date IS NULL OR c.called_at >= %s::date)
        AND (%s::date IS NULL OR c.called_at < %s::date)
        AND (%s::text IS NULL OR p.dir_name = %s)
 GROUP BY c.model
 ORDER BY total_tokens DESC',
    'Total tokens (input incl. cache reads/writes, plus output) grouped by model; optionally filtered by date range or project.',
    ARRAY['tokens', 'model', 'group by'],
    '[{"name": "since", "type": "since", "optional": true, "default": null}, {"name": "since", "type": "since", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "project", "type": "project", "optional": true, "default": null}, {"name": "project", "type": "project", "optional": true, "default": null}]'::jsonb
),
(
    'total tokens per project',
    'SELECT p.dir_name AS project_dir,
       sum(c.total_input_tokens) AS total_input_tokens,
       sum(c.output_tokens)      AS output_tokens
  FROM claude.api_calls c
       JOIN claude.sessions s ON s.session_id = c.session_id
       JOIN claude.projects p ON p.project_id = s.project_id
 WHERE (%s::date IS NULL OR c.called_at >= %s::date)
        AND (%s::date IS NULL OR c.called_at < %s::date)
        AND (%s::text IS NULL OR c.model = %s)
 GROUP BY p.dir_name
 ORDER BY total_input_tokens DESC NULLS LAST',
    'Billed input + output tokens per project; optionally filtered by date range or model.',
    ARRAY['tokens', 'project'],
    '[{"name": "since", "type": "since", "optional": true, "default": null}, {"name": "since", "type": "since", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "model", "type": "model", "optional": true, "default": null}, {"name": "model", "type": "model", "optional": true, "default": null}]'::jsonb
),
(
    'total tokens per day',
    'SELECT (c.called_at AT TIME ZONE ''UTC'')::date AS usage_date,
       sum(c.total_input_tokens) AS total_input_tokens,
       sum(c.output_tokens)      AS output_tokens,
       count(*)                  AS api_calls
  FROM claude.api_calls c
       JOIN claude.sessions s ON s.session_id = c.session_id
       JOIN claude.projects p ON p.project_id = s.project_id
 WHERE (%s::date IS NULL OR c.called_at >= %s::date)
        AND (%s::date IS NULL OR c.called_at < %s::date)
        AND (%s::text IS NULL OR p.dir_name = %s)
        AND (%s::text IS NULL OR c.model = %s)
 GROUP BY 1
 ORDER BY usage_date',
    'Daily token usage trend; optionally filtered by date range, project or model.',
    ARRAY['tokens', 'daily', 'trend'],
    '[{"name": "since", "type": "since", "optional": true, "default": null}, {"name": "since", "type": "since", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "project", "type": "project", "optional": true, "default": null}, {"name": "project", "type": "project", "optional": true, "default": null}, {"name": "model", "type": "model", "optional": true, "default": null}, {"name": "model", "type": "model", "optional": true, "default": null}]'::jsonb
),
(
    'top 10 sessions by total tokens',
    'SELECT s.session_id, s.project_dir, s.total_input_tokens, s.output_tokens
  FROM claude.v_session_token_summary s
 WHERE (%s::date IS NULL OR s.first_event_at >= %s::date)
        AND (%s::date IS NULL OR s.first_event_at < %s::date)
 ORDER BY s.total_input_tokens DESC NULLS LAST
 LIMIT %s::int',
    'The N sessions with the highest billed token usage (default 10); optionally filtered by session start date.',
    ARRAY['tokens', 'sessions', 'top'],
    '[{"name": "since", "type": "since", "optional": true, "default": null}, {"name": "since", "type": "since", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "limit", "type": "limit", "optional": true, "default": 10}]'::jsonb
),
(
    'top 10 projects by tokens',
    'SELECT p.dir_name AS project_dir,
       sum(c.total_input_tokens) AS total_input_tokens,
       sum(c.output_tokens)      AS output_tokens
  FROM claude.api_calls c
       JOIN claude.sessions s ON s.session_id = c.session_id
       JOIN claude.projects p ON p.project_id = s.project_id
 WHERE (%s::date IS NULL OR c.called_at >= %s::date)
        AND (%s::date IS NULL OR c.called_at < %s::date)
 GROUP BY p.dir_name
 ORDER BY total_input_tokens DESC NULLS LAST
 LIMIT %s::int',
    'The N projects with the highest billed token usage (default 10); optionally filtered by date range.',
    ARRAY['tokens', 'projects', 'top'],
    '[{"name": "since", "type": "since", "optional": true, "default": null}, {"name": "since", "type": "since", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "limit", "type": "limit", "optional": true, "default": 10}]'::jsonb
),
(
    'output tokens per model',
    'SELECT c.model, sum(c.output_tokens) AS output_tokens
  FROM claude.api_calls c
       JOIN claude.sessions s ON s.session_id = c.session_id
       JOIN claude.projects p ON p.project_id = s.project_id
 WHERE (%s::date IS NULL OR c.called_at >= %s::date)
        AND (%s::date IS NULL OR c.called_at < %s::date)
        AND (%s::text IS NULL OR p.dir_name = %s)
 GROUP BY c.model
 ORDER BY output_tokens DESC',
    'Output tokens grouped by model; optionally filtered by date range or project.',
    ARRAY['tokens', 'model', 'output'],
    '[{"name": "since", "type": "since", "optional": true, "default": null}, {"name": "since", "type": "since", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "project", "type": "project", "optional": true, "default": null}, {"name": "project", "type": "project", "optional": true, "default": null}]'::jsonb
),
(
    'daily api call trend',
    'SELECT (c.called_at AT TIME ZONE ''UTC'')::date AS usage_date,
       count(*)                     AS api_calls,
       count(DISTINCT c.session_id) AS sessions
  FROM claude.api_calls c
       JOIN claude.sessions s ON s.session_id = c.session_id
       JOIN claude.projects p ON p.project_id = s.project_id
 WHERE (%s::date IS NULL OR c.called_at >= %s::date)
        AND (%s::date IS NULL OR c.called_at < %s::date)
        AND (%s::text IS NULL OR p.dir_name = %s)
        AND (%s::text IS NULL OR c.model = %s)
 GROUP BY 1
 ORDER BY usage_date',
    'Number of API calls and active sessions per day; optionally filtered by date range, project or model.',
    ARRAY['api_calls', 'daily', 'trend'],
    '[{"name": "since", "type": "since", "optional": true, "default": null}, {"name": "since", "type": "since", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "project", "type": "project", "optional": true, "default": null}, {"name": "project", "type": "project", "optional": true, "default": null}, {"name": "model", "type": "model", "optional": true, "default": null}, {"name": "model", "type": "model", "optional": true, "default": null}]'::jsonb
),
(
    'what is the total number of tokens overall',
    'SELECT coalesce(sum(c.total_input_tokens), 0) AS total_input_tokens,
       coalesce(sum(c.output_tokens), 0)      AS output_tokens
  FROM claude.api_calls c
       JOIN claude.sessions s ON s.session_id = c.session_id
       JOIN claude.projects p ON p.project_id = s.project_id
 WHERE (%s::date IS NULL OR c.called_at >= %s::date)
        AND (%s::date IS NULL OR c.called_at < %s::date)
        AND (%s::text IS NULL OR p.dir_name = %s)
        AND (%s::text IS NULL OR c.model = %s)',
    'Grand total of billed input and output tokens; optionally filtered by date range, project or model.',
    ARRAY['tokens', 'total', 'overall'],
    '[{"name": "since", "type": "since", "optional": true, "default": null}, {"name": "since", "type": "since", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "project", "type": "project", "optional": true, "default": null}, {"name": "project", "type": "project", "optional": true, "default": null}, {"name": "model", "type": "model", "optional": true, "default": null}, {"name": "model", "type": "model", "optional": true, "default": null}]'::jsonb
),
(
    'how much money did I spend in total',
    'SELECT sum(c.cost_usd)                            AS cost_usd,
       count(*) FILTER (WHERE c.cost_usd IS NULL) AS unpriced_calls,
       count(*)                                   AS api_calls
  FROM claude.v_api_call_cost c
       JOIN claude.sessions s ON s.session_id = c.session_id
       JOIN claude.projects p ON p.project_id = s.project_id
 WHERE (%s::date IS NULL OR c.called_at >= %s::date)
        AND (%s::date IS NULL OR c.called_at < %s::date)
        AND (%s::text IS NULL OR p.dir_name = %s)
        AND (%s::text IS NULL OR c.model = %s)',
    'Total USD cost (money spent) across all calls; optionally filtered by date range, project or model. cost_usd ignores unpriced calls (no model_pricing row); unpriced_calls counts them.',
    ARRAY['cost', 'money', 'usd', 'spend', 'total'],
    '[{"name": "since", "type": "since", "optional": true, "default": null}, {"name": "since", "type": "since", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "project", "type": "project", "optional": true, "default": null}, {"name": "project", "type": "project", "optional": true, "default": null}, {"name": "model", "type": "model", "optional": true, "default": null}, {"name": "model", "type": "model", "optional": true, "default": null}]'::jsonb
),
(
    'what is the cost per model',
    'SELECT c.model,
       sum(c.cost_usd)                            AS cost_usd,
       count(*) FILTER (WHERE c.cost_usd IS NULL) AS unpriced_calls,
       count(*)                                   AS api_calls
  FROM claude.v_api_call_cost c
       JOIN claude.sessions s ON s.session_id = c.session_id
       JOIN claude.projects p ON p.project_id = s.project_id
 WHERE (%s::date IS NULL OR c.called_at >= %s::date)
        AND (%s::date IS NULL OR c.called_at < %s::date)
        AND (%s::text IS NULL OR p.dir_name = %s)
 GROUP BY c.model
 ORDER BY cost_usd DESC NULLS LAST',
    'USD cost (money spent) per model; optionally filtered by date range or project. cost_usd ignores unpriced calls (no model_pricing row); unpriced_calls counts them.',
    ARRAY['cost', 'money', 'usd', 'spend', 'model', 'group by'],
    '[{"name": "since", "type": "since", "optional": true, "default": null}, {"name": "since", "type": "since", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "project", "type": "project", "optional": true, "default": null}, {"name": "project", "type": "project", "optional": true, "default": null}]'::jsonb
),
(
    'what is the cost per project',
    'SELECT p.dir_name AS project_dir,
       sum(c.cost_usd)                            AS cost_usd,
       count(*) FILTER (WHERE c.cost_usd IS NULL) AS unpriced_calls,
       count(*)                                   AS api_calls
  FROM claude.v_api_call_cost c
       JOIN claude.sessions s ON s.session_id = c.session_id
       JOIN claude.projects p ON p.project_id = s.project_id
 WHERE (%s::date IS NULL OR c.called_at >= %s::date)
        AND (%s::date IS NULL OR c.called_at < %s::date)
        AND (%s::text IS NULL OR c.model = %s)
 GROUP BY p.dir_name
 ORDER BY cost_usd DESC NULLS LAST',
    'USD cost (money spent) per project; optionally filtered by date range or model. cost_usd ignores unpriced calls (no model_pricing row); unpriced_calls counts them.',
    ARRAY['cost', 'money', 'usd', 'spend', 'project', 'group by'],
    '[{"name": "since", "type": "since", "optional": true, "default": null}, {"name": "since", "type": "since", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "model", "type": "model", "optional": true, "default": null}, {"name": "model", "type": "model", "optional": true, "default": null}]'::jsonb
),
(
    'what is the cost per day',
    'SELECT (c.called_at AT TIME ZONE ''UTC'')::date AS usage_date,
       sum(c.cost_usd)                            AS cost_usd,
       count(*) FILTER (WHERE c.cost_usd IS NULL) AS unpriced_calls,
       count(*)                                   AS api_calls
  FROM claude.v_api_call_cost c
       JOIN claude.sessions s ON s.session_id = c.session_id
       JOIN claude.projects p ON p.project_id = s.project_id
 WHERE (%s::date IS NULL OR c.called_at >= %s::date)
        AND (%s::date IS NULL OR c.called_at < %s::date)
        AND (%s::text IS NULL OR p.dir_name = %s)
        AND (%s::text IS NULL OR c.model = %s)
 GROUP BY 1
 ORDER BY usage_date',
    'Daily USD cost (money spent) trend; optionally filtered by date range, project or model. cost_usd ignores unpriced calls (no model_pricing row); unpriced_calls counts them.',
    ARRAY['cost', 'money', 'usd', 'spend', 'daily', 'trend'],
    '[{"name": "since", "type": "since", "optional": true, "default": null}, {"name": "since", "type": "since", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "project", "type": "project", "optional": true, "default": null}, {"name": "project", "type": "project", "optional": true, "default": null}, {"name": "model", "type": "model", "optional": true, "default": null}, {"name": "model", "type": "model", "optional": true, "default": null}]'::jsonb
),
(
    'how much did this session cost',
    'SELECT sum(c.cost_usd)                            AS cost_usd,
       count(*) FILTER (WHERE c.cost_usd IS NULL) AS unpriced_calls,
       count(*)                                   AS api_calls
  FROM claude.v_api_call_cost c
 WHERE c.session_id = %s',
    'USD cost (money spent) of one session. cost_usd ignores unpriced calls (no model_pricing row); unpriced_calls counts them.',
    ARRAY['session_scoped', 'cost', 'money', 'usd', 'spend', 'session'],
    '[{"name": "session_id", "type": "session_id"}]'::jsonb
),
(
    'top 10 most expensive sessions',
    'SELECT c.session_id, p.dir_name AS project_dir,
       sum(c.cost_usd)                            AS cost_usd,
       count(*) FILTER (WHERE c.cost_usd IS NULL) AS unpriced_calls,
       count(*)                                   AS api_calls
  FROM claude.v_api_call_cost c
       JOIN claude.sessions s ON s.session_id = c.session_id
       JOIN claude.projects p ON p.project_id = s.project_id
 WHERE (%s::date IS NULL OR c.called_at >= %s::date)
        AND (%s::date IS NULL OR c.called_at < %s::date)
 GROUP BY c.session_id, p.dir_name
 ORDER BY cost_usd DESC NULLS LAST
 LIMIT %s::int',
    'The N sessions with the highest USD cost (default 10); optionally filtered by date range. cost_usd ignores unpriced calls (no model_pricing row); unpriced_calls counts them.',
    ARRAY['cost', 'money', 'usd', 'spend', 'sessions', 'top'],
    '[{"name": "since", "type": "since", "optional": true, "default": null}, {"name": "since", "type": "since", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "limit", "type": "limit", "optional": true, "default": 10}]'::jsonb
),
(
    'which calls have no price',
    'SELECT c.model, count(*) AS unpriced_calls
  FROM claude.v_api_call_cost c
 WHERE (%s::date IS NULL OR c.called_at >= %s::date)
        AND (%s::date IS NULL OR c.called_at < %s::date)
   AND c.cost_usd IS NULL
 GROUP BY c.model
 ORDER BY unpriced_calls DESC',
    'Count of calls with no model_pricing row (cost_usd NULL), per model; optionally filtered by date range.',
    ARRAY['cost', 'money', 'usd', 'spend', 'unpriced', 'pricing'],
    '[{"name": "since", "type": "since", "optional": true, "default": null}, {"name": "since", "type": "since", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}]'::jsonb
),
(
    'which model is used least',
    'SELECT model, count(*) AS api_calls
  FROM claude.api_calls
 WHERE model IS NOT NULL
 GROUP BY model
 ORDER BY api_calls ASC, model
 LIMIT 1',
    'The least-used model by number of API calls.',
    ARRAY['model', 'least used'],
    '[]'::jsonb
),
(
    'what is the overall cache write percentage',
    'SELECT round(coalesce(sum(cache_write_tokens), 0)::numeric
             / NULLIF(sum(total_input_tokens), 0) * 100, 2) AS cache_write_pct
  FROM claude.api_calls',
    'Overall percentage of billed input tokens written to cache.',
    ARRAY['cache', 'percentage', 'write'],
    '[]'::jsonb
),
(
    'what is the average number of tokens per session',
    'SELECT round(avg(total_input_tokens)) AS avg_input_tokens,
       round(avg(output_tokens))      AS avg_output_tokens
  FROM claude.v_session_token_summary
 WHERE api_calls > 0',
    'Average billed input and output tokens per session (sessions with at least one call).',
    ARRAY['tokens', 'average', 'sessions'],
    '[]'::jsonb
),
(
    'how many main transcript sessions are there',
    'SELECT count(DISTINCT session_id) AS main_session_count
  FROM claude.transcripts
 WHERE kind = ''session''',
    'Number of sessions that have a main (non-subagent) transcript.',
    ARRAY['count', 'sessions', 'main', 'transcripts'],
    '[]'::jsonb
)
ON CONFLICT (question)
DO UPDATE SET sql_text    = EXCLUDED.sql_text,
              description = EXCLUDED.description,
              tags        = EXCLUDED.tags,
              slots       = EXCLUDED.slots;
