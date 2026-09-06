-- Session-grain context-window and pacing metrics for the Grafana dashboards.
--
-- Idempotent (CREATE OR REPLACE) so `acp init-db` can replay it safely.
-- Depends on 003_views.sql (claude.v_call_cache_efficiency), which already
-- computes per-call context_growth_tokens via lag(total_input_tokens) over
-- (PARTITION BY transcript_id ORDER BY call_index). This file deliberately does
-- NOT widen that view: 003 recreates it with CREATE OR REPLACE on every replay,
-- and CREATE OR REPLACE VIEW cannot drop columns, so a widened view would make
-- the next `init-db` fail in 003. New columns live here instead.

-- ---------------------------------------------------------------------------
-- Per-call view: existing per-transcript context growth, plus the gap to the
-- previous call *in the same session* (across transcripts).
-- ---------------------------------------------------------------------------
-- api_calls.delta_seconds is a per-transcript gap; when a session interleaves a
-- main transcript with subagent transcripts, the session-level pause a human
-- would recognise is the gap between consecutive called_at values regardless of
-- transcript. session_gap_seconds is that reading. The first call of a session
-- has no predecessor and is NULL, so it never counts as a zero-length gap.
CREATE OR REPLACE VIEW claude.v_call_context_growth AS
SELECT
    e.session_id,
    e.transcript_id,
    e.call_index,
    e.called_at,
    e.delta_seconds,
    e.total_input_tokens,
    e.context_growth_tokens,
    EXTRACT(EPOCH FROM (e.called_at - lag(e.called_at) OVER w))::numeric
                                                            AS session_gap_seconds
FROM claude.v_call_cache_efficiency e
WINDOW w AS (
    PARTITION BY e.session_id
    ORDER BY e.called_at, e.transcript_id, e.call_index
);

COMMENT ON VIEW claude.v_call_context_growth IS
    'Per-API-call context metrics: total_input_tokens is the billed context window '
    'for that call, context_growth_tokens (from v_call_cache_efficiency) is its '
    'growth over the previous call in the same transcript, and session_gap_seconds '
    'is the wall-clock gap to the previous call anywhere in the same session.';

-- ---------------------------------------------------------------------------
-- One row per session: the dashboard-facing rollup.
-- ---------------------------------------------------------------------------
-- Kept separate from claude.v_session_token_summary on purpose -- that view is
-- DROP ... CASCADE'd and rebuilt by 003 and then redefined again by 008, so
-- adding columns to it would have to be done in two places and would still
-- break replay. Dashboards LEFT JOIN this view on session_id instead.
CREATE OR REPLACE VIEW claude.v_session_context_metrics AS
SELECT
    g.session_id,
    count(*)                                                AS api_calls,
    round(avg(g.total_input_tokens), 1)                     AS avg_context_window_tokens,
    max(g.total_input_tokens)                               AS max_context_window_tokens,
    round(avg(g.context_growth_tokens), 1)                  AS avg_context_growth,
    max(g.context_growth_tokens)                            AS max_context_growth,
    round(avg(g.session_gap_seconds), 1)                    AS avg_time_api_calls,
    round(max(g.session_gap_seconds), 1)                    AS max_time_api_calls
FROM claude.v_call_context_growth g
GROUP BY g.session_id;

COMMENT ON VIEW claude.v_session_context_metrics IS
    'Session-grain context and pacing rollup: average/peak billed context window '
    'per call, average/peak per-call context growth, and average/peak gap between '
    'consecutive calls in the session. max_time_api_calls uses cross-transcript '
    'called_at ordering, so it is not the same as '
    'v_session_token_summary.max_gap_seconds (a max over per-transcript '
    'delta_seconds).';

-- The grafana_reader default privileges from 007 already cover views created by
-- the owner role, but replaying this file against a DB provisioned before 007
-- would otherwise leave the new views unreadable.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_reader') THEN
        GRANT SELECT ON claude.v_call_context_growth      TO grafana_reader;
        GRANT SELECT ON claude.v_session_context_metrics  TO grafana_reader;
    END IF;
END
$$;
