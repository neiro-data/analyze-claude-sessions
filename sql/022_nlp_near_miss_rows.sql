-- Catalog rows for near-miss intents (see tests/data/ask_eval/test.yaml kind near_miss).
--
-- Each row answers a question that previously matched only a neighbour at a
-- different grouping, metric or granularity (per project vs per model, hour
-- vs day, shortest vs average). Descriptions state the contrast so the
-- retriever can tell the rows apart. Hour and week buckets use UTC to match
-- the per-day rows in sql/018. Upserts keyed on `question`, so replaying on
-- every `acp init-db` is safe. Keep eval questions out of the paraphrases:
-- tests/test_ask_eval.py fails if a test.yaml question appears here.

INSERT INTO claude.nlp_examples (question, sql_text, description, tags, slots, paraphrases) VALUES
(
    'what are the output tokens per project',
    'SELECT p.dir_name AS project_dir, sum(c.output_tokens) AS output_tokens
  FROM claude.api_calls c
       JOIN claude.sessions s ON s.session_id = c.session_id
       JOIN claude.projects p ON p.project_id = s.project_id
 WHERE (%s::date IS NULL OR c.called_at >= %s::date)
        AND (%s::date IS NULL OR c.called_at < %s::date)
 GROUP BY p.dir_name
 ORDER BY output_tokens DESC',
    'Output (generated) tokens summed per project; optionally filtered by date range.',
    ARRAY['tokens', 'projects', 'output', 'group by'],
    '[{"name": "since", "type": "since", "optional": true, "default": null}, {"name": "since", "type": "since", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}]'::jsonb,
    ARRAY['completion tokens for each project', 'output token count grouped by project', 'generated tokens in every project']
),
(
    'what is the average cost per session',
    'SELECT round(avg(s.cost_usd), 4) AS avg_cost_usd, count(*) AS priced_sessions
  FROM claude.v_session_token_summary s
 WHERE (%s::date IS NULL OR s.first_event_at >= %s::date)
        AND (%s::date IS NULL OR s.first_event_at < %s::date)
   AND s.cost_usd IS NOT NULL',
    'Mean dollar cost of a single session; optionally filtered by session start date.',
    ARRAY['cost', 'money', 'usd', 'sessions', 'average'],
    '[{"name": "since", "type": "since", "optional": true, "default": null}, {"name": "since", "type": "since", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}]'::jsonb,
    ARRAY['mean spend per session', 'typical dollar cost of one session', 'how much does a session cost on average']
),
(
    'what is the cache read percentage per model',
    'SELECT c.model,
       round(coalesce(sum(c.cache_read_tokens), 0)::numeric
             / NULLIF(sum(c.total_input_tokens), 0) * 100, 2) AS cache_read_pct
  FROM claude.api_calls c
 WHERE (%s::date IS NULL OR c.called_at >= %s::date)
        AND (%s::date IS NULL OR c.called_at < %s::date)
 GROUP BY c.model
 ORDER BY cache_read_pct DESC NULLS LAST',
    'Cache read share of input tokens for each model.',
    ARRAY['cache', 'percentage', 'model', 'group by'],
    '[{"name": "since", "type": "since", "optional": true, "default": null}, {"name": "since", "type": "since", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}]'::jsonb,
    ARRAY['share of input tokens served from cache by model', 'cache hit rate for each model', 'per model prompt cache reads percent']
),
(
    'what is the average subagent transcript duration',
    'SELECT round(avg(duration_seconds)::numeric, 1) AS avg_subagent_duration_seconds
  FROM claude.transcripts
 WHERE kind = ''subagent''',
    'Mean duration in seconds of a subagent transcript.',
    ARRAY['duration', 'average', 'subagent', 'transcripts'],
    '[]'::jsonb,
    ARRAY['mean runtime of subagent runs', 'how long do subagent transcripts last on average', 'typical sidechain transcript length in seconds']
),
(
    'how many api calls per project',
    'SELECT p.dir_name AS project_dir, count(*) AS api_calls
  FROM claude.api_calls c
       JOIN claude.sessions s ON s.session_id = c.session_id
       JOIN claude.projects p ON p.project_id = s.project_id
 WHERE (%s::date IS NULL OR c.called_at >= %s::date)
        AND (%s::date IS NULL OR c.called_at < %s::date)
 GROUP BY p.dir_name
 ORDER BY api_calls DESC',
    'Count of API calls for each project.',
    ARRAY['calls', 'projects', 'group by'],
    '[{"name": "since", "type": "since", "optional": true, "default": null}, {"name": "since", "type": "since", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}]'::jsonb,
    ARRAY['api call count by project', 'requests made in each project', 'model calls grouped by project']
),
(
    'tokens per hour',
    'SELECT date_trunc(''hour'', c.called_at AT TIME ZONE ''UTC'') AS usage_hour,
       sum(c.total_input_tokens) AS total_input_tokens,
       sum(c.output_tokens)      AS output_tokens
  FROM claude.api_calls c
 WHERE (%s::date IS NULL OR c.called_at >= %s::date)
        AND (%s::date IS NULL OR c.called_at < %s::date)
 GROUP BY 1
 ORDER BY usage_hour',
    'Token usage bucketed by UTC hour; optionally filtered by date range.',
    ARRAY['tokens', 'hourly'],
    '[{"name": "since", "type": "since", "optional": true, "default": null}, {"name": "since", "type": "since", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}]'::jsonb,
    ARRAY['hourly token usage', 'token totals for each hour', 'tokens bucketed by hour']
),
(
    'cost per hour',
    'SELECT date_trunc(''hour'', c.called_at AT TIME ZONE ''UTC'') AS usage_hour,
       sum(c.cost_usd) AS cost_usd
  FROM claude.v_api_call_cost c
 WHERE (%s::date IS NULL OR c.called_at >= %s::date)
        AND (%s::date IS NULL OR c.called_at < %s::date)
 GROUP BY 1
 ORDER BY usage_hour',
    'Dollar cost bucketed by UTC hour; optionally filtered by date range.',
    ARRAY['cost', 'money', 'usd', 'hourly'],
    '[{"name": "since", "type": "since", "optional": true, "default": null}, {"name": "since", "type": "since", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}]'::jsonb,
    ARRAY['hourly spend', 'dollar totals for each hour', 'money spent bucketed by hour']
),
(
    'tokens per week',
    'SELECT date_trunc(''week'', c.called_at AT TIME ZONE ''UTC'')::date AS usage_week,
       sum(c.total_input_tokens) AS total_input_tokens,
       sum(c.output_tokens)      AS output_tokens
  FROM claude.api_calls c
 WHERE (%s::date IS NULL OR c.called_at >= %s::date)
        AND (%s::date IS NULL OR c.called_at < %s::date)
 GROUP BY 1
 ORDER BY usage_week',
    'Token usage bucketed by UTC calendar week; optionally filtered by date range.',
    ARRAY['tokens', 'weekly'],
    '[{"name": "since", "type": "since", "optional": true, "default": null}, {"name": "since", "type": "since", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}]'::jsonb,
    ARRAY['weekly token usage', 'token totals for each calendar week', 'tokens bucketed by calendar week']
),
(
    'what are the input tokens per model',
    'SELECT c.model,
       sum(c.input_tokens)       AS input_tokens,
       sum(c.total_input_tokens) AS total_input_tokens
  FROM claude.api_calls c
 WHERE (%s::date IS NULL OR c.called_at >= %s::date)
        AND (%s::date IS NULL OR c.called_at < %s::date)
 GROUP BY c.model
 ORDER BY total_input_tokens DESC NULLS LAST',
    'Input tokens for each model (uncached and total billed input).',
    ARRAY['tokens', 'model', 'input', 'group by'],
    '[{"name": "since", "type": "since", "optional": true, "default": null}, {"name": "since", "type": "since", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}, {"name": "until", "type": "until", "optional": true, "default": null}]'::jsonb,
    ARRAY['prompt tokens by model', 'how many input tokens did each model consume', 'input side token counts for every model']
),
(
    'what is the shortest session duration',
    'SELECT min(duration_seconds) AS min_duration_seconds
  FROM claude.sessions
 WHERE duration_seconds IS NOT NULL',
    'Minimum session duration in seconds.',
    ARRAY['duration', 'minimum', 'sessions'],
    '[]'::jsonb,
    ARRAY['minimum length of a session in seconds', 'how brief is the briefest session', 'smallest session runtime']
),
(
    'what is the longest session duration',
    'SELECT max(duration_seconds) AS max_duration_seconds
  FROM claude.sessions
 WHERE duration_seconds IS NOT NULL',
    'Maximum session duration in seconds.',
    ARRAY['duration', 'maximum', 'sessions'],
    '[]'::jsonb,
    ARRAY['maximum length of a session in seconds', 'how long is the lengthiest session', 'biggest session runtime']
)
ON CONFLICT (question) DO UPDATE SET
    sql_text = EXCLUDED.sql_text,
    description = EXCLUDED.description,
    tags = EXCLUDED.tags,
    slots = EXCLUDED.slots,
    paraphrases = EXCLUDED.paraphrases;
