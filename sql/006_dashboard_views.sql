-- Dashboard support view: row-grain call detail for drill-down panels.
-- Unlike claude.v_daily_token_usage this is deliberately NOT time-bucketed --
-- it is meant to be filtered by session_id/transcript_id from a dashboard.

-- One row per API call, joined out to its transcript kind.
CREATE OR REPLACE VIEW claude.v_call_detail AS
SELECT
    c.session_id,
    c.transcript_id,
    t.kind                                                  AS kind,
    c.call_id,
    c.call_index,
    c.called_at,
    c.delta_seconds,
    c.agent,
    c.agent_type,
    c.depth,
    c.parent_agent,
    c.model,
    c.tool_use_id,
    c.description,
    c.input_tokens,
    c.cache_write_tokens,
    c.cache_read_tokens,
    c.output_tokens,
    c.total_input_tokens
FROM claude.api_calls c
JOIN claude.transcripts t USING (transcript_id);
