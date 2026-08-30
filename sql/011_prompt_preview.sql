-- First line (<=200 chars) of the user turn that triggered each API call.
-- Populated going forward only; historical rows stay NULL (no backfill).
ALTER TABLE claude.api_calls
    ADD COLUMN IF NOT EXISTS prompt_preview text;

COMMENT ON COLUMN claude.api_calls.prompt_preview IS
    'First non-empty line of the triggering user turn, whitespace-collapsed and '
    'truncated to 200 characters by cache_report_per_call_timing.sh. NULL for rows '
    'ingested before this column existed.';

-- Re-declare v_call_detail with the new column appended (mirrors sql/006_dashboard_views.sql).
CREATE OR REPLACE VIEW claude.v_call_detail AS
SELECT
    c.session_id,
    c.transcript_id,
    t.kind AS kind,
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
    c.total_input_tokens,
    c.prompt_preview
FROM claude.api_calls c
JOIN claude.transcripts t USING (transcript_id);
