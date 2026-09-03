# Scripts and commands guide

Referenced from [README.md](README.md). This file lists every command by component. For the
data model, see [SCHEMA.md](SCHEMA.md).

## PostgreSQL / Docker

```bash
make env          # writes .env with your real CLAUDE_PROJECTS_DIR
make build        # builds both images (first run compiles pg_parquet, ~10-20 min)
make up           # starts PostgreSQL, waits for healthy
make init-db      # applies sql/*.sql (also runs automatically on a fresh volume)
make psql          # opens a psql shell in the running container
make shell         # opens a shell in the app container
make export-parquet  # exports tables/views to data/parquet/ via pg_parquet
make down          # stops containers, keeps the data volume
make destroy       # stops containers AND deletes the data volume
make logs          # tails PostgreSQL logs
make ps            # shows container status
```

## Ingest

- `make ingest` — runs `bin/ingest.sh` inside the app container.
- `make ingest ARGS="--project billing"` — narrows to one project.
- `make ingest ARGS="--force"` — re-reports transcripts that are byte-identical to what is
  already loaded.
- `bin/ingest.sh [--project X | --force]` — the same, run directly; idempotent, safe to cron.
- `bin/backup-db.sh [DEST]` — backs up the DB (`pg_dump` custom format) and parquet exports
  to `DEST` (default `./data/backups`).
- `bin/prune-csv.sh [--days N] [--delete] --yes` — trims/archives the `data/csv/` audit
  folder; parquet + Postgres are the durable audit copy, so the raw CSVs are prunable.
- `make stats` — prints ingest summary stats.

### Scheduled ingest (macOS / launchd)

`bin/ingest.sh` is safe to run unattended, but nothing schedules it by default —
`make install-timer` is the explicit, one-time opt-in:

```bash
make install-timer     # installs + loads a launchd agent, runs bin/ingest.sh every 30 min
make uninstall-timer   # unloads + removes it
```

This writes `~/Library/LaunchAgents/com.analyze-claude-projects.ingest.plist` from
`bin/launchd/com.analyze-claude-projects.ingest.plist.template`, resolving the repo root and
`docker`'s directory into an explicit `PATH` (launchd jobs start with a minimal environment, no
shell profile). Logs go to `data/logs/ingest.launchd.{out,err}.log`; a completed run also shows
up in `claude.ingest_runs` (`acp query "select * from claude.ingest_runs order by run_id desc
limit 5"`). Requires Docker Desktop (or equivalent) to be running at the scheduled time —
`bin/ingest.sh` starts the `postgres` container itself but not the Docker daemon.

Check/trigger manually:

```bash
launchctl list | grep com.analyze-claude-projects.ingest
launchctl start com.analyze-claude-projects.ingest   # run once, right now
```

## Ad-hoc charts without the database (DuckDB)

With the stack **down**, `scripts/duckdb_parquet.py` opens the pg_parquet exports directly
via DuckDB's `read_parquet` — no PostgreSQL, no containers:

```bash
uv run python scripts/duckdb_parquet.py                    # example aggregations
uv run python scripts/duckdb_parquet.py --sql "SELECT model, count(*) FROM read_parquet('data/parquet/api_calls.parquet') GROUP BY model"
```

Requires `data/parquet/*.parquet` to already exist (`make export-parquet`, or any prior
`make ingest` run — export happens automatically at the end of the pipeline).

## RAG / NLP (`acp ask`)

Run it from the project root with `uv`:

```bash
uv run acp ask "how many tokens were spent per model?"
# scope a session-specific query with session_id=<id>, or just paste a bare UUID:
uv run acp ask "cache report session_id=1500e4c2-9021-4496-860e-5f99ec8ee7f5"
uv run acp ask "1500e4c2-9021-4496-860e-5f99ec8ee7f5 cache report"
```

(`acp` also lives **inside the app container** — `docker compose run --rm app ask "…"`,
or `make shell` then `acp ask "…"` — if you'd rather not run it on the host.)

It matches your question against a curated catalog of SQL queries (`claude.nlp_examples`,
seeded by `sql/005_nlp_catalog.sql`) using local sentence embeddings, then runs the best
match read-only. Fully offline — no network access or API key. Add `--sql` to see the
matched query, `--no-execute` to only preview it. If nothing matches confidently, it prints
the closest candidate questions instead of guessing.

**Slot-aware matching.** Each catalog row declares the filters its SQL can actually bind
(`session_id`, `since`, `project`, `model`, `limit` — see the `slots` column). Your question
is parsed for the same filters (`session_id=<id>` or a bare UUID; `since 2026-01[-15]`;
`last N days`; `last month`, meaning the previous calendar month, not a trailing 30-day
window; a known project or model name; `top N`). If the best-matching row can't bind a
filter your question carries — e.g. "tokens per model **last month**" matching the all-time
"tokens per model" template — `ask` **refuses** rather than silently running the unfiltered
query, and prints the near-matches instead:

```
$ uv run acp ask "tokens per model last month"
Refusing: your question filters by since ('2026-02-01') but the closest match
"how many tokens were spent per model" has no such filter. Try one of these instead:
  [0.71] how many tokens were spent per model
  ...
```

The embedding model (numpy, sentence-transformers) lives in the optional `nlp` dependency
group. The app image ships without it, so `ask` prints an install hint if it is missing;
enable it with a one-time `uv sync --group nlp` (locally) or by rebuilding the image with
that group. Every other `acp` command works without it.

### Retrieval quality checks

```bash
uv run pytest -m slow tests/test_nlp_eval.py -s
```

Loads the real embedding model against `tests/data/nlp_eval.yaml` (paraphrases per catalog
entry, plus out-of-catalog questions) and prints recall@1, recall@3, and a false-accept
rate. Marked `slow` and excluded from `make check` / the default `pytest` run since it loads
a real model; run it by hand when changing retrieval and compare against the last printed
numbers.

### RAG stress suite (opt-in)

A heavier, real-model stress battery lives in `tests/test_nlp_stress_slow.py` (all tests
marked `slow`) and `scripts/rag_stress.py`. It measures perturbation robustness (typos,
casing, punctuation, filler, truncation, unicode noise, …) against the clean baseline,
sweeps `CONFIDENCE_THRESHOLD` from 0.30–0.75 against the in-catalog/out-of-catalog corpora,
checks retrieval latency at 100/1k/10k rows, and re-runs the `ensure_read_only` adversarial
corpus and slot-extraction fuzzing for regressions. `ensure_read_only` itself parses the
SQL with `pglast` (real PostgreSQL grammar) rather than pattern-matching it, so quoting and
escaping edge cases can't slip past it.

```bash
uv run --group nlp pytest -m slow tests/test_nlp_stress_slow.py -s
uv run --group nlp python scripts/rag_stress.py            # full battery, writes data/reports/
uv run --group nlp python scripts/rag_stress.py --quick    # small/fast sanity run
```

or `make rag-stress` for the full report. It's excluded from `make check` and the default
`pytest` run — same reasons as the eval above — and its reports (`data/reports/`) are
gitignored scratch output, not tracked artifacts.

## Grafana dashboards

```bash
make dashboard      # or: make up-grafana
```

Starts PostgreSQL plus a provisioned Grafana instance at
`http://localhost:${GRAFANA_PORT:-3000}` — datasource and dashboards (usage overview,
per-session detail, session lineage, multi-agent group sessions) are loaded automatically
from `docker/grafana/`, no manual import needed. Grafana runs with anonymous Editor access
and no login, by design for local single-user use; the port is bound to `127.0.0.1` only —
don't expose it beyond localhost.

```bash
make down           # stops PostgreSQL and Grafana, keeps the data volume
```

There is no separate Grafana stop command — `make dashboard`/`make up-grafana` and `make up`
share the same Compose project, so `make down` (or `make destroy` to also drop the volume)
stops both.

See [README.md — Dashboard](README.md#dashboard) for what each dashboard shows.

## Model pricing

`data/reference/model_pricing.csv` is the source of truth for Claude API list prices (see
[SCHEMA.md — Model pricing](SCHEMA.md#model-pricing-csv-source-of-truth)).

```bash
make pricing-refresh ARGS='--input data/reference/snapshots/pricing_<date>.md'
                    # diffs a fetched pricing-page snapshot into the CSV; add --dry-run to preview
make pricing-sync-db # loads the CSV into claude.model_pricing (append-only, idempotent)
```

To refresh: fetch `https://platform.claude.com/docs/en/about-claude/pricing` (an agent tool
like WebFetch, or a manual copy-paste), save the "Model pricing" table as markdown to
`data/reference/snapshots/pricing_<date>.md`, then run `make pricing-refresh`. Nothing is
written to the CSV unless a price actually changed.

## Development

```bash
make venv       # uv sync
make check      # ruff + mypy --strict + pytest
```

Ruff (100 cols, `E,F,I,W,UP,S,B`), `mypy --strict`, and the default test suite all pass. Tests
mock nothing away that matters: the report tests shell out to the real script (skipped if
`jq` is missing), and none of them need PostgreSQL.
