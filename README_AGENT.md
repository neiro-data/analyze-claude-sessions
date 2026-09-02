# Agent notes

Referenced from [README.md](README.md). Background for an AI agent working in this repo —
implementation history that a human user does not need for day-to-day use.

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

## Layout

```
docker/postgres/Dockerfile   two-stage pg_parquet build
docker/app/Dockerfile        python:3.12-slim + bash + jq + uv
docker-compose.yml           postgres service + app CLI (profile: cli)
sql/                         001 extensions … 013 duplicated tokens (see sql/ for full list)
scripts/                     vendored per-call timing report, DuckDB ad-hoc, RAG stress
src/analyze_claude_projects/
  config.py                  env-driven settings
  discovery.py                find transcripts (main + subagent)
  jsonl_meta.py                stream a transcript for metadata
  report.py                    drive the shell script, locate the CSV
  loader.py                    polars read + COPY + upsert
  pipeline.py                  per-transcript orchestration
  parquet_export.py            pg_parquet COPY TO
  db.py                        connections, migrations, run audit
  nlp.py / nlp_stress.py       acp ask retrieval + stress harness
  cli.py                       `acp` commands
tests/                       tests, no database required (see tests/ for current count)
```

When this drifts from the real tree, prefer `git ls-files` / `ls` over trusting this file —
update it here once you notice, but don't assume it's exhaustive.

## Docs map

- `README.md` — human quick start and concept overview.
- `scripts-guide.md` — every command, grouped by component.
- `SCHEMA.md` — data model: tables, views, pipeline diagram, verified run stats.
- `README_AGENT.md` (this file) — agent-only background.
