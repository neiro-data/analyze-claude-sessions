# HANDOFF

Originally written 2026-08-08; **reviewed 2026-08-25** against the current code. Everything
in `README.md` is built, running, and verified against the real `~/.claude/projects`
(250 transcripts, 5,029 API calls, 0 failures).

## Status at a glance (2026-08-25 review)

| Item | Status |
| --- | --- |
| 1. Git — nothing committed | **DONE** — repo now has commits + PRs. |
| 2. Symlinked subagent double-count | **OPEN** — still 1/147, low impact. |
| 3. No incremental "tail" ingest | **OPEN** — grown transcripts still fully re-reported. |
| 4. Nothing consumes the data | **OPEN (eased)** — per-call `model` now exists, so the cost-model join is no longer lossy. |
| 5. Per-call model not captured | **DONE** — `model`/`agent`/`depth` columns landed in `96cc8e4`. |
| 6. Upstream SIGPIPE bug (external script) | **OPEN (external)** — outside this repo. |

Operator scripts now exist for routine work: `bin/ingest.sh`, `bin/backup-db.sh`,
`bin/prune-csv.sh` (see `README.md` → Quick guide). `prune-csv.sh` is the answer to the
unbounded `data/csv/` growth: parquet + Postgres are the durable audit copy, so raw CSVs are
archived/pruned on a rolling window.

## What is *not* done

### 1. Git — committed (DONE)

Resolved: the repo now has commits on `main` and PRs are opened per feature. `.gitignore`
covers `.env`, `.venv/`, `data/csv/`, `data/parquet/`, `data/backups/`, and the tool caches.
`.env` still holds the default password `claude` (git-ignored) — change it if this ever
leaves the laptop.

### 2. Symlinked subagent transcripts can double-count

One subagent file in `billing-application` is a symlink pointing into a *different*
session's directory. It now resolves (the projects dir is mounted at its own absolute path
inside the container — see the comment in `docker-compose.yml`), but the design question is
open: the same agent transcript is attributed to whichever sessions link to it, so its
tokens are counted once per linking session.

Only 1 of 147 subagent files is affected today, so the totals are effectively unchanged.
If more appear, deduplicate on `transcripts.jsonl_sha256` and decide which session owns the
calls.

### 3. No incremental "tail" ingest

Re-running `acp ingest` skips transcripts whose sha256 is unchanged, but a transcript that
*grew* is fully re-reported and re-upserted. Fine at this size (full run ≈ 1 min); it will
not stay fine at 10k transcripts. The upserts are idempotent, so appending-only ingestion is
a safe optimisation when it is needed.

### 4. Nothing consumes the data yet

The schema and Parquet exports exist; no dashboard, notebook, or scheduled run does. The
obvious next steps, roughly in value order:

- **Cost model.** `api_calls` has the four token classes but no pricing. Add a
  `claude.model_pricing` table keyed on model + effective date, join through
  `transcripts.models`, and turn `v_*_token_summary` into currency. Note the join is lossy
  today: `models` is an array on the transcript, not a column on the call — see item 5.
- **DuckDB over the Parquet.** `data/parquet/` is already queryable without the database
  running; a notebook that reads it is the fastest path to charts.
- **Scheduled ingest.** `make ingest` is idempotent and safe to run on a cron / launchd
  timer.

### 5. Per-call model is now captured (DONE)

Resolved in `96cc8e4`: `cache_report_per_call_timing.sh` now emits `model` (plus `agent`,
`agent_type`, `depth`, `parent_agent`), and `claude.api_calls` has the matching columns
(`sql/002_schema.sql:175-192`, indexed on `model`). Per-call and per-model breakdowns are now
a plain column, no array join. This also un-blocks the cost model in item 4.

### 6. Upstream script still has the SIGPIPE bug

The three fixes described in `README.md` were applied to the **vendored copy** only.
`REPOS/scripts/cache_report_per_call_timing.sh` is untouched — in particular fix 3, where
`jq | head -1` under `pipefail` aborts with exit 141 on large transcripts. Worth porting
back if that script is used directly.

## Operational notes

- **The first `make build` is slow** (~10–20 min): it compiles `pg_parquet` and ~950 Rust
  crates. Rebuilds are near-instant thanks to BuildKit cache mounts on the cargo registry
  and target dir. If you bump `PG_PARQUET_VERSION` or `PG_MAJOR` in `.env`, you pay it again.
- **`make destroy` deletes the data volume.** `make down` keeps it.
- **Changing `sql/*.sql`:** the `/docker-entrypoint-initdb.d` mount only runs on an *empty*
  volume. Use `make init-db` to re-apply to a live database. The DDL is idempotent
  (`CREATE TABLE IF NOT EXISTS` / `CREATE OR REPLACE VIEW`), which means it will **not**
  alter an existing table's columns. Once real data is worth keeping, adopt a proper
  migration tool rather than editing `002_schema.sql` in place.
- **`src/`, `scripts/`, `sql/` are bind-mounted** into the app container, so code edits take
  effect without a rebuild. Rebuild (`make build`) before treating the image as a
  self-contained artifact.
- **Parquet permissions:** `make env` chmods `data/parquet` to 777 because the server writes
  there as uid 999. On a non-Docker-Desktop host you may need to align ownership instead.

## Sanity checks worth re-running

```sql
-- loaded rows must match what the transcript itself claims
SELECT count(*) FROM (
  SELECT t.transcript_id
    FROM claude.transcripts t LEFT JOIN claude.api_calls c USING (transcript_id)
   GROUP BY t.transcript_id, t.api_call_count
  HAVING t.api_call_count <> count(c.call_id)) x;   -- expect 0

-- most recent run
SELECT * FROM claude.ingest_runs ORDER BY run_id DESC LIMIT 1;

-- anything that failed to load
SELECT * FROM claude.v_ingest_status WHERE run_status <> 'succeeded';
```
