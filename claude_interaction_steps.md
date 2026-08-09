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
