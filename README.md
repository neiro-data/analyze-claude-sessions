# analyze-claude-projects

Docker foundation for analysing Claude Code session transcripts: PostgreSQL 17 with the
`pg_parquet` extension, a Python 3.12 ingestion CLI, and a star-ish schema over the
per-API-call token/timing reports produced by `cache_report_per_call_timing.sh`.

## Resume

- **Stack:** Docker Compose (PostgreSQL 17 + `pg_parquet` 0.5.1, Python 3.12 app), `uv`,
  `polars`, `psycopg` 3, `typer`, `ruff`, `mypy`, `pytest`.
- **What it does:** walks `~/.claude/projects/**/*.jsonl`, runs the vendored shell report per
  transcript to produce a CSV, and loads those CSVs plus transcript metadata into the
  `claude` schema in PostgreSQL. Exports any table or view to Parquet through `pg_parquet`.
- **Start here:** `make build` (slow the first time — it compiles `pg_parquet` from source),
  then `make ingest`, then `make stats`.
- **Everything runs in containers.** The host only needs Docker and `make`; `uv` is needed
  only for the local dev targets (`make check`).
- **Entry point:** `src/analyze_claude_projects/cli.py` (`acp` console script).
- **Schema:** `sql/002_schema.sql` (tables) and `sql/003_views.sql` (reporting views).

## Quick guide

### First-time setup

```bash
make env          # writes .env with your real CLAUDE_PROJECTS_DIR
make build        # builds both images (first run compiles pg_parquet, ~10-20 min)
make up           # starts PostgreSQL, waits for healthy
make init-db      # applies sql/*.sql (also runs automatically on a fresh volume)
```

### Routine ops

- `bin/ingest.sh [--project X | --force]` — ingest new transcripts and refresh parquet
  (idempotent; safe to cron).
- `bin/backup-db.sh [DEST]` — back up the DB (pg_dump custom format) and parquet exports
  to `DEST` (default `./data/backups`).
- `bin/prune-csv.sh [--days N] [--delete] --yes` — trim/archive the `data/csv/` audit
  folder; parquet + Postgres are the durable audit copy, so the raw CSVs are prunable.

`make ingest ARGS="--project billing"` narrows to one project;
`ARGS="--force"` re-reports transcripts that are byte-identical to what is already loaded
(both covered by `bin/ingest.sh`). `make stats`, `make psql`, and `make export-parquet`
are still available directly.

### Natural-language queries (`acp ask`)

Run it from the project root with `uv`:

```bash
uv run acp ask "how many tokens were spent per model?"
# scope a session-specific query with session_id=<id>:
uv run acp ask "cache report session_id=1500e4c2-9021-4496-860e-5f99ec8ee7f5"
```

(`acp` also lives **inside the app container** — `docker compose run --rm app ask "…"`,
or `make shell` then `acp ask "…"` — if you'd rather not run it on the host.)

It matches your question against a curated catalog of SQL queries (`claude.nlp_examples`,
seeded by `sql/005_nlp_catalog.sql`) using local sentence embeddings, then runs the best
match read-only. Fully offline — no network access or API key. Add `--sql` to see the
matched query, `--no-execute` to only preview it. If nothing matches confidently, it prints
the closest candidate questions instead of guessing.

The embedding model (numpy, sentence-transformers) lives in the optional `nlp` dependency
group. The app image ships without it, so `ask` prints an install hint if it is missing;
enable it with a one-time `uv sync --group nlp` (locally) or by rebuilding the image with
that group. Every other `acp` command works without it.

## Dashboard

`make dashboard` (or `make up-grafana`) starts PostgreSQL plus a provisioned Grafana instance
at `http://localhost:${GRAFANA_PORT:-3000}` — datasource and dashboards (usage overview,
per-session detail, session lineage) are loaded automatically from `docker/grafana/`, no manual
import needed.
Grafana runs with anonymous Editor access and no login, by design for local single-user use;
the port is bound to `127.0.0.1` only — don't expose it beyond localhost.

Both dashboards render in UTC and carry a `Model` filter (defaults to all models combined).
The overview shows token/call/duration totals plus time series and a session explorer; its
"Cache efficiency" panel stacks cache-write and cache-read token bars (right axis) under the
cache-read % line (left axis) — the bar total is cache tokens only, not total input tokens.
The session detail dashboard's session picker lists raw session UUIDs, and a header panel
above the stat row shows the AI-generated session title (falling back to the UUID when none
exists). It breaks a single session down per API call (tokens, cache-read %, context growth,
time between calls) with a Cache-read % color code (green ≥90%, yellow 60–90%, red <60%)
shared across both dashboards. The "Call timeline" panel stacks per-model token bars (one
colour per model) when Model = All. "Context growth by call" is colored blue for negative
growth, green 0–500 tokens, yellow 500–2k, orange 2k–10k, red >10k. "Time between calls" is
colored green <10s, yellow 10–30s, orange 30s–5m, red 5m–1h, dark blue >1h.

The session lineage dashboard tracks parent/child spawn chains: a session's `Origin
session_id: <uuid>` marker (left by the `clear-cache-sonnet-low` handoff at the start of a
child's first prompt) is captured into `sessions.parent_session_id`. It carries a `Role` filter
(`parent` / `child` / `standalone`, default all) alongside the shared `Model` filter, a stat row
(child/parent/family/orphan-child counts), a "Session families" table rolling up each chain's
length, models used, and total tokens end to end, and a "Sessions" table with data links on both
`session_id` and `parent_session_id`. `parent_session_id` is unvalidated and not a foreign key —
the parent may never get ingested — so `parent_ingested` distinguishes a resolved parent from a
merely claimed one. Wiring the role toggle into the other two dashboards is not done yet.
The "Session families" table has a `combined family view` column that is populated only when
`chain_length > 1`; clicking it opens the family dashboard below.

The session family dashboard (uid `acp-session-family`) renders a whole spawn chain as one
merged session — the Session Detail layout, but over every member at once. Pick a family with
the `Family (root session)` variable (only chains with `chain_length > 1` are listed), then use
the `Scope` toggle to switch between *All sessions in the family* and *Sessions 1..N-1 (drop
most recent)* — useful for asking what the chain had cost before the newest handoff. Calls from
all included sessions are merged into a single timestamp-ordered timeline (`family_call_index`
is recomputed after the scope filter, so it stays contiguous under either setting), and the
"Merged call timeline" panel gives each member its own colour with a legend; the "Family
members" table doubles as the legend and links each member out to its own Session Detail.
Backed by `claude.v_session_family_member` and `claude.v_family_call_detail`
(`sql/009_family_dashboard_views.sql`). Members are ordered by first activity, so a member that was ingested but made no API call has no `first_event_at` and sorts last — the "drop most recent" toggle would drop that empty session rather than the true latest handoff.

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

### The transcript grain

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

## Changes to the vendored report script

`scripts/cache_report_per_call_timing.sh` is a copy of `REPOS/scripts/…` with three fixes.
The first two were needed to match the reference CSV and run on Linux; the third is a real
bug that also affects the original:

1. **Emits `parentUuid`.** The reference CSV has this column; upstream does not produce it.
2. **Portable filename timestamp.** Upstream uses BSD-only `date -j -f`, which silently
   falls back to *now* on Linux. Replaced with a `sed` transform, so macOS and the
   container agree.
3. **No more SIGPIPE.** `jq -r '.timestamp' "$f" | head -1` under `set -o pipefail` aborts
   the script with exit 141 once the file is large enough for `jq` to still be writing when
   `head` exits. This killed ~5 of the biggest transcripts. Replaced with
   `jq -rn 'first(inputs | .timestamp // empty)'`, which short-circuits inside `jq`.

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

## Layout

```
docker/postgres/Dockerfile   two-stage pg_parquet build
docker/app/Dockerfile        python:3.12-slim + bash + jq + uv
docker-compose.yml           postgres service + app CLI (profile: cli)
sql/                         001 extensions, 002 schema, 003 views
scripts/                     vendored per-call timing report
src/analyze_claude_projects/
  config.py                  env-driven settings
  discovery.py               find transcripts (main + subagent)
  jsonl_meta.py              stream a transcript for metadata
  report.py                  drive the shell script, locate the CSV
  loader.py                  polars read + COPY + upsert
  pipeline.py                per-transcript orchestration
  parquet_export.py          pg_parquet COPY TO
  db.py                      connections, migrations, run audit
  cli.py                     `acp` commands
tests/                       21 tests, no database required
```

## Development

```bash
make venv       # uv sync
make check      # ruff + mypy --strict + pytest
```

Ruff (100 cols, `E,F,I,W,UP,S,B`), `mypy --strict`, and 21 tests all pass. Tests mock nothing
away that matters: the report tests shell out to the real script (skipped if `jq` is
missing), and none of them need PostgreSQL.

See `HANDOFF.md` for what to do next.
