-- Prompt-length (long-context) pricing tiers. A model may carry one tier: when a call's
-- total_input_tokens (input + cache write + cache read) is strictly greater than the
-- threshold, the whole request is priced at the long_context_* rates. Additive and
-- idempotent; cost is computed on read, so no backfill is needed.
ALTER TABLE claude.model_pricing
    ADD COLUMN IF NOT EXISTS long_context_threshold_tokens integer
        CHECK (long_context_threshold_tokens > 0),
    ADD COLUMN IF NOT EXISTS long_context_input_usd_per_mtok numeric(10, 4)
        CHECK (long_context_input_usd_per_mtok >= 0),
    ADD COLUMN IF NOT EXISTS long_context_output_usd_per_mtok numeric(10, 4)
        CHECK (long_context_output_usd_per_mtok >= 0),
    ADD COLUMN IF NOT EXISTS long_context_cache_write_usd_per_mtok numeric(10, 4)
        CHECK (long_context_cache_write_usd_per_mtok >= 0),
    ADD COLUMN IF NOT EXISTS long_context_cache_write_1h_usd_per_mtok numeric(10, 4)
        CHECK (long_context_cache_write_1h_usd_per_mtok >= 0),
    ADD COLUMN IF NOT EXISTS long_context_cache_read_usd_per_mtok numeric(10, 4)
        CHECK (long_context_cache_read_usd_per_mtok >= 0);

-- All-or-none: a threshold needs the four rates the views read (1h may stay NULL).
-- PG has no ADD CONSTRAINT IF NOT EXISTS -> guard via pg_constraint.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'model_pricing_long_context_all_or_none'
                     AND conrelid = 'claude.model_pricing'::regclass) THEN
        ALTER TABLE claude.model_pricing
            ADD CONSTRAINT model_pricing_long_context_all_or_none CHECK (
                (long_context_threshold_tokens IS NULL
                    AND long_context_input_usd_per_mtok IS NULL
                    AND long_context_output_usd_per_mtok IS NULL
                    AND long_context_cache_write_usd_per_mtok IS NULL
                    AND long_context_cache_read_usd_per_mtok IS NULL)
                OR
                (long_context_threshold_tokens IS NOT NULL
                    AND long_context_input_usd_per_mtok IS NOT NULL
                    AND long_context_output_usd_per_mtok IS NOT NULL
                    AND long_context_cache_write_usd_per_mtok IS NOT NULL
                    AND long_context_cache_read_usd_per_mtok IS NOT NULL));
    END IF;
END $$;

COMMENT ON COLUMN claude.model_pricing.long_context_threshold_tokens IS
    'Strictly greater than; compared with api_calls.total_input_tokens, which includes '
    'cache reads and writes. NULL = no long-context tier.';
COMMENT ON COLUMN claude.model_pricing.long_context_input_usd_per_mtok IS
    'Input rate for calls above long_context_threshold_tokens.';
COMMENT ON COLUMN claude.model_pricing.long_context_output_usd_per_mtok IS
    'Output rate for calls above long_context_threshold_tokens.';
COMMENT ON COLUMN claude.model_pricing.long_context_cache_write_usd_per_mtok IS
    '5-minute cache-write rate for calls above long_context_threshold_tokens.';
COMMENT ON COLUMN claude.model_pricing.long_context_cache_write_1h_usd_per_mtok IS
    '1-hour cache-write long-context rate. Stored only; the cost views do not use it.';
COMMENT ON COLUMN claude.model_pricing.long_context_cache_read_usd_per_mtok IS
    'Cache-read rate for calls above long_context_threshold_tokens.';

-- ---------------------------------------------------------------------------
-- Views: same column list/types as 008 and 014; only the rate pick changes.
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
        c.input_tokens       * mp.input_rate       / 1e6
      + c.cache_write_tokens * mp.cache_write_rate / 1e6
      + c.cache_read_tokens  * mp.cache_read_rate  / 1e6
      + c.output_tokens      * mp.output_rate      / 1e6
    END::numeric(12, 6)                                     AS cost_usd
FROM claude.api_calls c
LEFT JOIN LATERAL (
    SELECT p.model, p.effective_from,
           CASE WHEN t.lc THEN p.long_context_input_usd_per_mtok       ELSE p.input_usd_per_mtok       END AS input_rate,
           CASE WHEN t.lc THEN p.long_context_cache_write_usd_per_mtok ELSE p.cache_write_usd_per_mtok END AS cache_write_rate,
           CASE WHEN t.lc THEN p.long_context_cache_read_usd_per_mtok  ELSE p.cache_read_usd_per_mtok  END AS cache_read_rate,
           CASE WHEN t.lc THEN p.long_context_output_usd_per_mtok      ELSE p.output_usd_per_mtok      END AS output_rate
    FROM claude.model_pricing p
    CROSS JOIN LATERAL (
        SELECT (p.long_context_threshold_tokens IS NOT NULL
                AND c.total_input_tokens > p.long_context_threshold_tokens) AS lc
    ) t
    WHERE p.model = c.model
      AND p.effective_from <= c.called_at::date
    ORDER BY p.effective_from DESC
    LIMIT 1
) mp ON true;

COMMENT ON VIEW claude.v_api_call_cost IS
    'Per-call cost in USD via a lateral pick of the newest model_pricing row with '
    'effective_from <= the call date. cost_usd is NULL, never 0, when the model has '
    'no priced row as of that date. If the picked row has a long-context tier and '
    'api_calls.total_input_tokens > its threshold, all four token categories are '
    'priced at the long_context_* rates.';

CREATE OR REPLACE VIEW claude.v_api_call_cost_duplicated AS
SELECT
    c.call_id,
    c.transcript_id,
    c.session_id,
    c.called_at,
    c.model,
    mp.effective_from                                       AS pricing_effective_from,
    CASE WHEN mp.model IS NULL THEN NULL ELSE
        c.usage_records * (
            c.input_tokens       * mp.input_rate
          + c.cache_write_tokens * mp.cache_write_rate
          + c.cache_read_tokens  * mp.cache_read_rate
        ) / 1e6
      + c.output_tokens_raw * mp.output_rate / 1e6
    END::numeric(12, 6)                                     AS cost_usd
FROM claude.api_calls c
LEFT JOIN LATERAL (
    SELECT p.model, p.effective_from,
           CASE WHEN t.lc THEN p.long_context_input_usd_per_mtok       ELSE p.input_usd_per_mtok       END AS input_rate,
           CASE WHEN t.lc THEN p.long_context_cache_write_usd_per_mtok ELSE p.cache_write_usd_per_mtok END AS cache_write_rate,
           CASE WHEN t.lc THEN p.long_context_cache_read_usd_per_mtok  ELSE p.cache_read_usd_per_mtok  END AS cache_read_rate,
           CASE WHEN t.lc THEN p.long_context_output_usd_per_mtok      ELSE p.output_usd_per_mtok      END AS output_rate
    FROM claude.model_pricing p
    CROSS JOIN LATERAL (
        SELECT (p.long_context_threshold_tokens IS NOT NULL
                AND c.total_input_tokens > p.long_context_threshold_tokens) AS lc
    ) t
    WHERE p.model = c.model
      AND p.effective_from <= c.called_at::date
    ORDER BY p.effective_from DESC
    LIMIT 1
) mp ON true;

COMMENT ON VIEW claude.v_api_call_cost_duplicated IS
    'Same lateral price pick as v_api_call_cost, but priced against the '
    'non-deduplicated /stats arithmetic (usage_records, output_tokens_raw from 013) '
    'instead of the deduplicated truth. Expected to run higher than v_api_call_cost -- '
    'that is correct, not a bug. cost_usd is NULL, never 0, for an unpriced model. '
    'The long-context tier test is per request (total_input_tokens > threshold).';
