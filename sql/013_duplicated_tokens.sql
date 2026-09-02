-- Adds the two extra facts needed to reproduce Claude Code's own `/stats`
-- arithmetic (dailyModelTokens in ~/.claude/stats-cache.json), which sums
-- output_tokens across every JSONL record in a requestId group without
-- dedup. claude.api_calls already stores the deduplicated truth
-- (input + write + read + max(output)); this migration adds the raw group
-- size and raw output sum, plus a generated column reproducing the
-- non-deduplicated total, purely additive and alongside the true numbers.
-- Idempotent so it can be replayed safely.

ALTER TABLE claude.api_calls
    ADD COLUMN IF NOT EXISTS usage_records integer NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS output_tokens_raw integer NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'api_calls_usage_records_check'
    ) THEN
        ALTER TABLE claude.api_calls
            ADD CONSTRAINT api_calls_usage_records_check CHECK (usage_records > 0);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'api_calls_output_tokens_raw_check'
    ) THEN
        ALTER TABLE claude.api_calls
            ADD CONSTRAINT api_calls_output_tokens_raw_check CHECK (output_tokens_raw >= 0);
    END IF;
END $$;

ALTER TABLE claude.api_calls
    ADD COLUMN IF NOT EXISTS duplicated_total_tokens integer
    GENERATED ALWAYS AS (
        usage_records * (input_tokens + cache_write_tokens + cache_read_tokens)
        + output_tokens_raw
    ) STORED;

COMMENT ON COLUMN claude.api_calls.usage_records IS
    'Number of JSONL records collapsed into this requestId group. Exists to reproduce the non-deduplicated arithmetic of Claude Code''s own /stats (dailyModelTokens in ~/.claude/stats-cache.json). NOT the true per-API-call usage.';
COMMENT ON COLUMN claude.api_calls.output_tokens_raw IS
    'Sum of output_tokens across every JSONL record in this requestId group (vs. output_tokens = max, the deduplicated truth). Exists to reproduce /stats'' non-deduplicated arithmetic. NOT the true per-API-call usage.';
COMMENT ON COLUMN claude.api_calls.duplicated_total_tokens IS
    'usage_records * (input + cache_write + cache_read) + output_tokens_raw: reproduces the non-deduplicated total Claude Code''s /stats reports. NOT the true per-API-call usage -- total_input_tokens + output_tokens remains the truth.';
