# Schema and data model

Referenced from [README.md](README.md#schema).

## How it fits together

```
~/.claude/projects/**/*.jsonl
        │
        ├─ jsonl_meta.py ──── streamed once for metadata (sha256, models, record histogram)
        │
        └─ scripts/cache_report_per_call_timing.sh   (bash + jq)
                   │
                   └─ data/csv/<session_id>/<stem>_<YYYYMMDDTHH:MM:SS>.csv
                              │
                              └─ loader.py (polars → COPY → upsert) → PostgreSQL
                                                                          │
                                                                          └─ pg_parquet
                                                                             → data/parquet/
```

## The transcript grain

A session owns **more than one** `.jsonl` file:

| Layout | Meaning |
| --- | --- |
| `<project>/<session_id>.jsonl` | main session transcript |
| `<project>/<session_id>/subagents/agent-<id>.jsonl` | one file per subagent it spawned |

Subagent files repeat the parent's `sessionId` but contain a **disjoint** set of
`requestId`s — verified: zero overlap. They are real, additional API calls, and on this
machine they account for **49% of all billed input tokens**. So the unit that maps 1:1 onto
a CSV is `claude.transcripts`, not `claude.sessions`; a session is a rollup over its
transcripts.

## Schema (`claude`)

**Fact**

| Table | Grain | Notes |
| --- | --- | --- |
| `api_calls` | one Claude API call (one `requestId` group) | `call_index`…`delta_seconds` map 1:1 onto the CSV columns; `total_input_tokens` is a stored generated column |

**Metadata**

| Table | Grain |
| --- | --- |
| `projects` | one directory under `~/.claude/projects` |
| `sessions` | one Claude Code session, rolled up from its transcripts; `parent_session_id` is the claimed origin session (unvalidated, no FK) |
| `transcripts` | one `.jsonl` file (`kind` = `session` \| `subagent`), with size, sha256, time window, models, CLI versions, git branches, per-type record counts |
| `transcript_record_types` | `(transcript_id, type, subtype)` histogram |
| `model_pricing` | one `(model, effective_from)` price point; append-only, loaded from `data/reference/model_pricing.csv` via `acp pricing sync-db` |

**Audit**

| Table | Grain |
| --- | --- |
| `ingest_runs` | one `acp ingest` invocation: counts, status, error text |
| `csv_files` | one generated CSV: path, sha256, row count, owning run |
| `parquet_exports` | one `pg_parquet` export |
| `migrations` | which `sql/*.sql` was applied, and when |

### `/stats` vs. deduplicated tokens

A single API call can log multiple JSONL
records under one `requestId` (one per assistant content block); `input_tokens`,
`cache_creation_input_tokens` and `cache_read_input_tokens` are identical across
that group, but `output_tokens` differs per record. `api_calls`' core columns
dedupe the group (input/write/read taken once, `output_tokens = max(...)`) — the
true per-call usage. Claude Code's own `/stats` (`dailyModelTokens` in
`~/.claude/stats-cache.json`) doesn't dedupe: it sums `output_tokens` across every
record with no grouping. `usage_records` (group size), `output_tokens_raw`
(`sum(output)`), and the generated `duplicated_total_tokens` column
(`usage_records * (input + write + read) + output_tokens_raw`) reproduce that
inflated `/stats` arithmetic purely for comparison — they don't change the
deduplicated truth in the other columns.

**Views** — `v_transcript_token_summary`, `v_session_token_summary`,
`v_project_token_summary`, `v_daily_token_usage`, `v_subagent_share`,
`v_call_cache_efficiency` (per-call cache % and context growth),
`v_long_gaps` (gaps ≥ 5 min), `v_ingest_status`, `v_session_lineage` (per-session
parent/child role, resolved root and depth, cycle-safe), `v_session_family` (per-root chain
rollup: length, models, total calls/tokens, first→last span), `v_api_call_cost` (per-call
list-price estimate, deduplicated tokens), `v_api_call_cost_duplicated` (same estimate priced
against the `/stats`-style inflated arithmetic above), `v_call_context_growth` (per-call billed
context window, per-transcript growth, and cross-transcript session gap),
`v_session_context_metrics` (session rollup of the above: `avg/max_context_window_tokens`,
`avg/max_context_growth`, `avg/max_time_api_calls`), `v_session_size_flags` (per-session
`is_small` flag: duration < 30s or fewer than 5 API calls, backing the Usage overview
dashboard's `Small sessions` filter).

### Model pricing (CSV source of truth)

`data/reference/model_pricing.csv` is the human-auditable price list — Claude API list prices
scraped from `platform.claude.com/docs/en/about-claude/pricing`, one row per
`(model, ingested_date)`, append-only (a rate change is a new row, never an edit).
`data/reference/model_id_map.csv` maps DB snapshot ids (e.g.
`claude-haiku-4-5-20251001`) to their base CSV model id.

- `acp pricing refresh --input <snapshot.md>` diffs a fetched pricing-page snapshot against
  the CSV and appends only the rows whose price actually changed.
- `acp pricing sync-db` loads the CSV's latest-per-model prices into `claude.model_pricing`,
  append-only: it never overwrites a standard rate. The one exception is a fill-only
  `ON CONFLICT (model, effective_from) DO UPDATE` that sets the long-context tier columns on
  an existing row whose tier is still `NULL` (it never overwrites a tier that is already set).
  Filled models are reported as `Filled long-context tier`. If a set tier differs from the CSV,
  the row is left unchanged and the model is listed in a warning; a tier change needs a new
  `effective_from` row.
- `v_api_call_cost` / `v_api_call_cost_duplicated` and the three summary views pick the
  newest `model_pricing` row with `effective_from <= called_at::date` — a call before any
  priced row, or for an unpriced model, costs `NULL`, never `0`.
- **Long-context tiers.** `model_pricing` has optional `long_context_threshold_tokens` plus
  `long_context_{input,output,cache_write,cache_write_1h,cache_read}_usd_per_mtok` columns
  (CSV: `long_context_*` appended after `source_url`; empty = no tier). When the picked row has a
  tier and `api_calls.total_input_tokens` (input + cache write + cache read) is **strictly
  greater** than the threshold, the whole request (input, cache write, cache read, output) is
  priced at the long rates, in both cost views. A check constraint makes the tier all-or-none
  (the 1h long rate may stay `NULL`; it is stored, not used by the views). `pricing refresh`
  merges the page's "up to N tokens" / "over N tokens" row pairs into one row.
- These are **list-price estimates**, not a Claude Pro bill.

## `pg_parquet`

CrunchyData ships no binaries or images for `pg_parquet`, so `docker/postgres/Dockerfile`
builds it from a pinned source tag with `cargo-pgrx` in stage 1 and copies only the
`.so` / `.control` / `.sql` into a clean `postgres:17-bookworm` in stage 2. Pins live in
`.env` (`PG_MAJOR=17`, `PG_PARQUET_VERSION=0.5.1`, `PGRX_VERSION=0.16.0`). The extension is
in `shared_preload_libraries` both via the image's `postgresql.conf.sample` and via an
explicit `command:` flag in compose, so it survives a config override.

`COPY ... TO` runs **inside the server**, so exports target the path the container sees
(`/parquet`, bind-mounted to `./data/parquet`). Reads work too:

```sql
SELECT * FROM parquet.schema('/parquet/api_calls.parquet');
SELECT * FROM parquet.file_metadata('/parquet/api_calls.parquet');
COPY some_table FROM '/parquet/api_calls.parquet' WITH (format 'parquet');
```

## Verified run

Against 250 transcripts in `~/.claude/projects` (2026-08-08):

| | |
| --- | --- |
| Projects / sessions / transcripts | 11 / 103 / 250 (147 subagent) |
| API calls loaded | 5,029 (0 failures) |
| Billed input tokens | 324,912,925 |
| Output tokens | 3,088,590 |
| Cache read share of input | 95.96% |
| Subagent share of input tokens | 49.15% |

Integrity checks all pass: `csv_files` = transcripts with calls (225 = 225), no transcript
whose loaded row count disagrees with its transcript-derived `api_call_count`, no orphan
calls. Parquet round-trip (export 5,029 rows → `COPY FROM` → re-sum) matches exactly.
