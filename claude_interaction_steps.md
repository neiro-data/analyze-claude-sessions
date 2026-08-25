# claude_interaction_steps

## 2026-08-08 — bootstrap analyze-claude-projects (no branch; repo git init'd, nothing committed)

Purpose: Docker foundation (PostgreSQL + pg_parquet + Python 3.12) that analyses
`~/.claude/projects/**/*.jsonl` via `cache_report_per_call_timing.sh` and loads the CSVs
into PostgreSQL.

Pipeline: not triggered — folder was empty and not a git repo (new-project bootstrap, which
CLAUDE.md routes outside the mandatory in-repo agent pipeline). No agents used.

Steps:
- Inspected the report script + reference CSV; found the CSV has a `parentUuid` column the
  script does not emit.
- No prebuilt pg_parquet image exists → two-stage Dockerfile building v0.5.1 with
  cargo-pgrx 0.16.0 against postgres:17-bookworm.
- Vendored the report script with 3 fixes: `parentUuid` column, portable filename timestamp
  (BSD-only `date -j -f`), and SIGPIPE/exit-141 on large files from `jq | head -1` under
  `pipefail`.
- Schema `claude`: api_calls (fact) + projects/sessions/transcripts/record-type metadata +
  ingest_runs/csv_files/parquet_exports/migrations audit + 8 views.
- **Re-grained mid-build**: discovery initially found only 103 of 250 transcripts. Subagent
  files live at `<project>/<session>/subagents/agent-*.jsonl`, share the parent sessionId,
  and have zero requestId overlap → real extra calls, 49% of billed input tokens. Introduced
  `claude.transcripts` between sessions and api_calls.
- Mounted the projects dir at its own absolute host path in the container so absolute
  symlink targets in subagent dirs resolve.
- Verified: 250/250 transcripts, 5,029 calls, 0 failures; Parquet export + `COPY FROM`
  round-trip matches; ruff + mypy --strict + 21 tests clean.
- Wrote README.md and HANDOFF.md.

## add-agent-columns-and-sessions-tables (2026-08-09)

Branch purpose: support the new per-call CSV format (parentUuid → agent/agent_type/depth/
parent_agent/model/tool_use_id/description/delta_seconds_global) and add session-level
subagent/model tracking + an upload audit table.

- Full pipeline run (user-confirmed): architect-orchestrator plan → python-pro implement →
  python-conventions → test-automator → code-reviewer.
- Recreated `claude.api_calls` for the new columns (sql/002 + new sql/004_agent_columns.sql
  upgrade migration); vendored the rewritten `cache_report_per_call_timing.sh` (now merges
  subagent transcripts into one per-session CSV).
- Extended existing `claude.sessions` (has_subagent, first_model) instead of creating a
  duplicate table — clarified with user first. Added new `claude.interactions_upload`
  audit table, filled by pipeline.py's ingest loop.
- code-reviewer found 3 real defects: subagent transcripts were being double-ingested
  (script now merges them, pipeline.py still reported on them directly too), session
  new/updated counters not rolled back on transaction failure, O(n²) delta_agent in the
  shell script. All three fixed by python-pro, re-verified by test-automator +
  code-reviewer spot check.
- ruff/mypy/pytest (22/22) all green. Committed, pushed, PR #1 opened into main
  (reviewer: neiro-data).

## 2026-08-25 — operator scripts (backup/ingest/prune) + HANDOFF review

Purpose: add host-side ops tooling and answer the unbounded data/csv/ growth. Delivered as
one PR per goal (user's call). Full agent pipeline run (user's call).
- Pipeline: plan skipped (already scoped); docker-expert implemented 3 bash scripts +
  README/.gitignore; python-conventions skipped (no Python); test-automator (static + help +
  prune dry-run, data/csv/ proven byte-identical); code-reviewer scoped to prune-csv.sh only
  (user's /btw). Review found 4 defects in prune-csv.sh (1 high --days 0 boundary, 1 med
  archive-before-delete, 2 low); all fixed inline + re-tested (scratch archive round-trip).
- PRs: #2 add-backup-script (backup-db.sh, DB pg_dump -Fc + parquet), this branch
  add-ingest-script (ingest.sh + README Quick guide), refresh-handoff (HANDOFF.md), and
  add-csv-prune (prune-csv.sh). CSV growth answer: parquet+Postgres are the durable audit
  copy, so raw CSVs are archived/pruned on a rolling window.
