-- Cost model: currency, not just tokens. `model_pricing` is a seeded, operator-maintained
-- table (Anthropic first-party API rates change over time and are not tracked anywhere
-- else in this schema) -- treat the seed rows below as a starting point, not truth.
CREATE TABLE IF NOT EXISTS claude.model_pricing (
    model                   text        NOT NULL,
    effective_from          date        NOT NULL,
    input_usd_per_mtok      numeric(10, 4) NOT NULL CHECK (input_usd_per_mtok       >= 0),
    output_usd_per_mtok     numeric(10, 4) NOT NULL CHECK (output_usd_per_mtok      >= 0),
    cache_write_usd_per_mtok numeric(10, 4) NOT NULL CHECK (cache_write_usd_per_mtok >= 0),
    cache_read_usd_per_mtok numeric(10, 4) NOT NULL CHECK (cache_read_usd_per_mtok  >= 0),
    source                  text,
    created_at              timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (model, effective_from)
);

COMMENT ON TABLE claude.model_pricing IS
    'Operator-maintained price list, not authoritative. Anthropic first-party API rates '
    'per million tokens; one row per (model, effective_from) so a rate change is a new '
    'row, never an update. Cost views pick the newest row with effective_from <= the '
    'call timestamp -- a call before any priced row, or for an unpriced model, must '
    'cost NULL, never 0.';
COMMENT ON COLUMN claude.model_pricing.model IS
    'Matches claude.api_calls.model verbatim, including date-suffixed snapshots '
    '(e.g. claude-haiku-4-5-20251001) -- add one row per snapshot actually seen.';
COMMENT ON COLUMN claude.model_pricing.source IS
    'Free-text provenance, e.g. "anthropic.com/pricing 2026-06-24".';

-- Seed: current public first-party pricing for the models actually present in
-- claude.api_calls as of this migration. `<synthetic>` (test/fixture rows) is
-- deliberately left unpriced -- its cost must come back NULL.
INSERT INTO claude.model_pricing
    (model, effective_from, input_usd_per_mtok, output_usd_per_mtok,
     cache_write_usd_per_mtok, cache_read_usd_per_mtok, source)
VALUES
    ('claude-sonnet-5', '2026-01-01', 2.00, 10.00, 2.50, 0.20,
     'anthropic.com/pricing, seeded 2026-08-29'),
    ('claude-opus-5', '2026-01-01', 5.00, 25.00, 6.25, 0.50,
     'anthropic.com/pricing, seeded 2026-08-29'),
    ('claude-opus-4-8', '2026-01-01', 5.00, 25.00, 6.25, 0.50,
     'anthropic.com/pricing, seeded 2026-08-29'),
    ('claude-haiku-4-5-20251001', '2026-01-01', 1.00, 5.00, 1.25, 0.10,
     'anthropic.com/pricing, seeded 2026-08-29')
ON CONFLICT (model, effective_from) DO NOTHING;

COMMENT ON COLUMN claude.model_pricing.cache_write_usd_per_mtok IS
    'Seed uses the standard 1.25x-of-input 5-minute cache-write rate; verify against '
    'the live price list before relying on it for billing-accurate figures.';
COMMENT ON COLUMN claude.model_pricing.cache_read_usd_per_mtok IS
    'Seed uses the standard 0.1x-of-input cache-read rate; verify against the live '
    'price list before relying on it for billing-accurate figures.';

-- ---------------------------------------------------------------------------
-- Cost-bearing views. Per-call cost is computed once here (call price to
-- picoUSD before aggregating) and reused so the summary views stay simple
-- sums, not weighted-average approximations.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW claude.v_api_call_cost AS
SELECT
    c.call_id,
    c.transcript_id,
    c.session_id,
    c.called_at,
    c.model,
    c.input_tokens,
    c.cache_write_tokens,
    c.cache_read_tokens,
    c.output_tokens,
    mp.effective_from                                       AS pricing_effective_from,
    CASE WHEN mp.model IS NULL THEN NULL ELSE
        c.input_tokens       * mp.input_usd_per_mtok       / 1e6
      + c.cache_write_tokens * mp.cache_write_usd_per_mtok / 1e6
      + c.cache_read_tokens  * mp.cache_read_usd_per_mtok  / 1e6
      + c.output_tokens      * mp.output_usd_per_mtok      / 1e6
    END                                                      AS cost_usd
FROM claude.api_calls c
LEFT JOIN LATERAL (
    SELECT p.*
    FROM claude.model_pricing p
    WHERE p.model = c.model
      AND p.effective_from <= c.called_at::date
    ORDER BY p.effective_from DESC
    LIMIT 1
) mp ON true;

COMMENT ON VIEW claude.v_api_call_cost IS
    'Per-call cost in USD via a lateral pick of the newest model_pricing row with '
    'effective_from <= the call date. cost_usd is NULL, never 0, when the model has '
    'no priced row as of that date.';

CREATE OR REPLACE VIEW claude.v_transcript_token_summary AS
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
    coalesce(sum(c.output_tokens), 0)                        AS output_tokens,
    coalesce(sum(c.total_input_tokens), 0)                  AS total_input_tokens,
    round(
        sum(c.cache_read_tokens)::numeric
        / NULLIF(sum(c.total_input_tokens), 0) * 100, 2)    AS cache_read_pct,
    sum(cc.cost_usd)                                        AS cost_usd
FROM claude.transcripts t
JOIN claude.sessions s USING (session_id)
JOIN claude.projects p USING (project_id)
LEFT JOIN claude.api_calls c USING (transcript_id)
LEFT JOIN claude.v_api_call_cost cc ON cc.call_id = c.call_id
GROUP BY t.transcript_id, t.session_id, t.kind, t.agent_id, p.dir_name,
         t.first_event_at, t.duration_seconds;

CREATE OR REPLACE VIEW claude.v_session_token_summary AS
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
    coalesce(sum(c.output_tokens), 0)                        AS output_tokens,
    coalesce(sum(c.total_input_tokens), 0)                  AS total_input_tokens,
    round(
        sum(c.cache_read_tokens)::numeric
        / NULLIF(sum(c.total_input_tokens), 0) * 100, 2)    AS cache_read_pct,
    round(avg(c.delta_seconds)::numeric, 1)                 AS avg_gap_seconds,
    max(c.delta_seconds)                                    AS max_gap_seconds,
    sum(cc.cost_usd)                                        AS cost_usd
FROM claude.sessions s
JOIN claude.projects p USING (project_id)
LEFT JOIN claude.transcripts t USING (session_id)
LEFT JOIN claude.api_calls c USING (transcript_id)
LEFT JOIN claude.v_api_call_cost cc ON cc.call_id = c.call_id
GROUP BY s.session_id, p.dir_name, p.project_path, s.ai_title, s.models,
         s.transcript_count, s.subagent_count, s.first_event_at, s.last_event_at,
         s.duration_seconds;

CREATE OR REPLACE VIEW claude.v_project_token_summary AS
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
    coalesce(sum(c.output_tokens), 0)                        AS output_tokens,
    coalesce(sum(c.total_input_tokens), 0)                  AS total_input_tokens,
    round(
        sum(c.cache_read_tokens)::numeric
        / NULLIF(sum(c.total_input_tokens), 0) * 100, 2)    AS cache_read_pct,
    min(s.first_event_at)                                   AS first_event_at,
    max(s.last_event_at)                                    AS last_event_at,
    sum(cc.cost_usd)                                        AS cost_usd
FROM claude.projects p
LEFT JOIN claude.sessions s USING (project_id)
LEFT JOIN claude.transcripts t USING (session_id)
LEFT JOIN claude.api_calls c USING (transcript_id)
LEFT JOIN claude.v_api_call_cost cc ON cc.call_id = c.call_id
GROUP BY p.project_id, p.dir_name, p.project_path;
