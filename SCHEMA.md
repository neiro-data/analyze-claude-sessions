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
rollup: length, models, total calls/tokens, first→last span).

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
