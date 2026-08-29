-- Reporting views. All are thin wrappers over claude.api_calls so they stay
-- correct as new transcripts land.

-- Per-transcript rollup: the finest grain that maps to one CSV.
-- DROP + CREATE (not OR REPLACE): 010_model_pricing.sql redefines this view with
-- an extra cost_usd column, and CREATE OR REPLACE VIEW can't drop columns -- so a
-- second full `make init-db` run would fail here trying to shrink back to this
-- shape. CASCADE is safe: only 005/008/009/010 depend on it, and all rerun after
-- this file on every pass.
DROP VIEW IF EXISTS claude.v_transcript_token_summary CASCADE;
CREATE VIEW claude.v_transcript_token_summary AS
SELECT
    t.transcript_id,
    t.session_id,
    t.kind,
    t.agent_id,
    p.dir_name                                              AS project_dir,
    t.first_event_at,
    t.duration_seconds,
    count(c.*)                                              AS api_calls,
    coalesce(sum(c.input_tokens), 0)                        AS input_tokens,
    coalesce(sum(c.cache_write_tokens), 0)                  AS cache_write_tokens,
    coalesce(sum(c.cache_read_tokens), 0)                   AS cache_read_tokens,
    coalesce(sum(c.output_tokens), 0)                       AS output_tokens,
    coalesce(sum(c.total_input_tokens), 0)                  AS total_input_tokens,
    round(
        sum(c.cache_read_tokens)::numeric
        / NULLIF(sum(c.total_input_tokens), 0) * 100, 2)    AS cache_read_pct
FROM claude.transcripts t
JOIN claude.sessions s USING (session_id)
JOIN claude.projects p USING (project_id)
LEFT JOIN claude.api_calls c USING (transcript_id)
GROUP BY t.transcript_id, t.session_id, t.kind, t.agent_id, p.dir_name,
         t.first_event_at, t.duration_seconds;

-- Per-session rollup, main transcript and subagents combined.
-- DROP + CREATE: see v_transcript_token_summary above -- 010_model_pricing.sql
-- adds a cost_usd column, so OR REPLACE would fail to shrink back on rerun.
DROP VIEW IF EXISTS claude.v_session_token_summary CASCADE;
CREATE VIEW claude.v_session_token_summary AS
SELECT
    s.session_id,
    p.dir_name                                              AS project_dir,
    p.project_path,
    s.ai_title,
    s.models,
    s.transcript_count,
    s.subagent_count,
    s.first_event_at,
    s.last_event_at,
    s.duration_seconds,
    count(c.*)                                              AS api_calls,
    count(*) FILTER (WHERE t.kind = 'subagent' AND c.call_id IS NOT NULL)
                                                            AS subagent_api_calls,
    coalesce(sum(c.input_tokens), 0)                        AS input_tokens,
    coalesce(sum(c.cache_write_tokens), 0)                  AS cache_write_tokens,
    coalesce(sum(c.cache_read_tokens), 0)                   AS cache_read_tokens,
    coalesce(sum(c.output_tokens), 0)                       AS output_tokens,
    coalesce(sum(c.total_input_tokens), 0)                  AS total_input_tokens,
    round(
        sum(c.cache_read_tokens)::numeric
        / NULLIF(sum(c.total_input_tokens), 0) * 100, 2)    AS cache_read_pct,
    round(avg(c.delta_seconds)::numeric, 1)                 AS avg_gap_seconds,
    max(c.delta_seconds)                                    AS max_gap_seconds
FROM claude.sessions s
JOIN claude.projects p USING (project_id)
LEFT JOIN claude.transcripts t USING (session_id)
LEFT JOIN claude.api_calls c USING (transcript_id)
GROUP BY s.session_id, p.dir_name, p.project_path, s.ai_title, s.models,
         s.transcript_count, s.subagent_count, s.first_event_at, s.last_event_at,
         s.duration_seconds;

-- Per-project rollup.
-- DROP + CREATE: see v_transcript_token_summary above -- 010_model_pricing.sql
-- adds a cost_usd column, so OR REPLACE would fail to shrink back on rerun.
DROP VIEW IF EXISTS claude.v_project_token_summary CASCADE;
CREATE VIEW claude.v_project_token_summary AS
SELECT
    p.project_id,
    p.dir_name                                              AS project_dir,
    p.project_path,
    count(DISTINCT s.session_id)                            AS sessions,
    count(DISTINCT t.transcript_id)                         AS transcripts,
    count(c.*)                                              AS api_calls,
    coalesce(sum(c.input_tokens), 0)                        AS input_tokens,
    coalesce(sum(c.cache_write_tokens), 0)                  AS cache_write_tokens,
    coalesce(sum(c.cache_read_tokens), 0)                   AS cache_read_tokens,
    coalesce(sum(c.output_tokens), 0)                       AS output_tokens,
    coalesce(sum(c.total_input_tokens), 0)                  AS total_input_tokens,
    round(
        sum(c.cache_read_tokens)::numeric
        / NULLIF(sum(c.total_input_tokens), 0) * 100, 2)    AS cache_read_pct,
    min(s.first_event_at)                                   AS first_event_at,
    max(s.last_event_at)                                    AS last_event_at
FROM claude.projects p
LEFT JOIN claude.sessions s USING (project_id)
LEFT JOIN claude.transcripts t USING (session_id)
LEFT JOIN claude.api_calls c USING (transcript_id)
GROUP BY p.project_id, p.dir_name, p.project_path;

-- Daily usage, for trend lines.
CREATE OR REPLACE VIEW claude.v_daily_token_usage AS
SELECT
    (c.called_at AT TIME ZONE 'UTC')::date                  AS usage_date,
    count(*)                                                AS api_calls,
    count(DISTINCT c.session_id)                            AS sessions,
    sum(c.input_tokens)                                     AS input_tokens,
    sum(c.cache_write_tokens)                               AS cache_write_tokens,
    sum(c.cache_read_tokens)                                AS cache_read_tokens,
    sum(c.output_tokens)                                    AS output_tokens,
    sum(c.total_input_tokens)                               AS total_input_tokens
FROM claude.api_calls c
GROUP BY 1;

-- Main-transcript vs subagent split, the question the extra grain exists to answer.
CREATE OR REPLACE VIEW claude.v_subagent_share AS
SELECT
    t.kind,
    count(*)                                                AS api_calls,
    sum(c.total_input_tokens)                               AS total_input_tokens,
    sum(c.output_tokens)                                    AS output_tokens,
    round(100.0 * sum(c.total_input_tokens)
          / NULLIF(sum(sum(c.total_input_tokens)) OVER (), 0), 2)
                                                            AS pct_of_input_tokens
FROM claude.api_calls c
JOIN claude.transcripts t USING (transcript_id)
GROUP BY t.kind;

-- Cache behaviour per call, with how much the billed context grew since the last one.
CREATE OR REPLACE VIEW claude.v_call_cache_efficiency AS
SELECT
    c.session_id,
    c.transcript_id,
    c.call_index,
    c.called_at,
    c.delta_seconds,
    c.input_tokens,
    c.cache_write_tokens,
    c.cache_read_tokens,
    c.output_tokens,
    c.total_input_tokens,
    round(c.cache_read_tokens::numeric
          / NULLIF(c.total_input_tokens, 0) * 100, 2)       AS cache_read_pct,
    c.total_input_tokens
        - lag(c.total_input_tokens) OVER w                  AS context_growth_tokens
FROM claude.api_calls c
WINDOW w AS (PARTITION BY c.transcript_id ORDER BY c.call_index);

-- Gaps long enough to look like a human pause or a stalled turn.
CREATE OR REPLACE VIEW claude.v_long_gaps AS
SELECT
    c.session_id,
    p.dir_name          AS project_dir,
    t.kind,
    c.call_index,
    c.called_at,
    c.delta_seconds
FROM claude.api_calls c
JOIN claude.transcripts t USING (transcript_id)
JOIN claude.sessions s ON s.session_id = c.session_id
JOIN claude.projects p USING (project_id)
WHERE c.delta_seconds >= 300
ORDER BY c.delta_seconds DESC;

-- Latest ingestion attempt per transcript, for spotting stale or failed loads.
CREATE OR REPLACE VIEW claude.v_ingest_status AS
SELECT DISTINCT ON (f.transcript_id)
    f.transcript_id,
    f.session_id,
    t.kind,
    f.run_id,
    r.status            AS run_status,
    r.command,
    f.csv_path,
    f.row_count,
    f.loaded_at,
    t.jsonl_sha256,
    t.record_count
FROM claude.csv_files f
JOIN claude.transcripts t USING (transcript_id)
LEFT JOIN claude.ingest_runs r USING (run_id)
ORDER BY f.transcript_id, f.loaded_at DESC;
