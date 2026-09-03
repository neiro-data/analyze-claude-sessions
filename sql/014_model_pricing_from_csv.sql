-- Widens claude.model_pricing so it can hold everything the CSV source of
-- truth (data/reference/model_pricing.csv) tracks, without touching the view
-- chain in 008. Purely additive and idempotent, mirroring 013's style: no
-- re-seed here -- `acp pricing sync-db` is what loads CSV rows, via
-- INSERT ... ON CONFLICT DO NOTHING against (model, effective_from), and it
-- never issues an UPDATE against an existing priced row.
ALTER TABLE claude.model_pricing
    ADD COLUMN IF NOT EXISTS cache_write_1h_usd_per_mtok numeric(10, 4)
        CHECK (cache_write_1h_usd_per_mtok >= 0),
    ADD COLUMN IF NOT EXISTS retired boolean NOT NULL DEFAULT false;

COMMENT ON TABLE claude.model_pricing IS
    'Price list loaded from data/reference/model_pricing.csv (source of truth), via '
    '`acp pricing sync-db`. One row per (model, effective_from) so a rate change is a '
    'new row, never an update. Cost views pick the newest row with '
    'effective_from <= the call timestamp -- a call before any priced row, or for an '
    'unpriced model, must cost NULL, never 0.';
COMMENT ON COLUMN claude.model_pricing.cache_write_1h_usd_per_mtok IS
    'Published 1-hour cache-write rate, nullable. cache_write_usd_per_mtok stays the '
    '5-minute rate the existing cost views read -- this column is additional data, '
    'not a replacement.';
COMMENT ON COLUMN claude.model_pricing.retired IS
    'True when the model carries a "(retired...)" suffix in the docs pricing table. '
    'Retired models keep their priced rows: historical sessions still need them.';

-- ---------------------------------------------------------------------------
-- /stats-arithmetic cost counterpart to v_api_call_cost, priced by the same
-- lateral pick, reading the duplicated-tokens facts added in 013. NULL when
-- the model has no priced row as of the call date -- never coerced to 0.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS claude.v_api_call_cost_duplicated;
CREATE VIEW claude.v_api_call_cost_duplicated AS
SELECT
    c.call_id,
    c.transcript_id,
    c.session_id,
    c.called_at,
    c.model,
    mp.effective_from                                       AS pricing_effective_from,
    CASE WHEN mp.model IS NULL THEN NULL ELSE
        c.usage_records * (
            c.input_tokens       * mp.input_usd_per_mtok
          + c.cache_write_tokens * mp.cache_write_usd_per_mtok
          + c.cache_read_tokens  * mp.cache_read_usd_per_mtok
        ) / 1e6
      + c.output_tokens_raw * mp.output_usd_per_mtok / 1e6
    END::numeric(12, 6)                                     AS cost_usd
FROM claude.api_calls c
LEFT JOIN LATERAL (
    SELECT p.*
    FROM claude.model_pricing p
    WHERE p.model = c.model
      AND p.effective_from <= c.called_at::date
    ORDER BY p.effective_from DESC
    LIMIT 1
) mp ON true;

COMMENT ON VIEW claude.v_api_call_cost_duplicated IS
    'Same lateral price pick as v_api_call_cost, but priced against the '
    'non-deduplicated /stats arithmetic (usage_records, output_tokens_raw from 013) '
    'instead of the deduplicated truth. Expected to run higher than v_api_call_cost -- '
    'that is correct, not a bug. cost_usd is NULL, never 0, for an unpriced model.';
