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

## Ingest

- `make ingest` — runs `bin/ingest.sh` inside the app container.
- `make ingest ARGS="--project billing"` — narrows to one project.
- `make ingest ARGS="--force"` — re-reports transcripts that are byte-identical to what is
  already loaded.
- `bin/ingest.sh [--project X | --force]` — the same, run directly; idempotent, safe to cron
  (see [Scheduled ingest](#scheduled-ingest-macos--launchd---optional), optional).
- `bin/backup-db.sh [DEST]` — backs up the DB (`pg_dump` custom format) and parquet exports
  to `DEST` (default `./data/backups`).
- `bin/prune-csv.sh [--days N] [--delete] --yes` — trims/archives the `data/csv/` audit
  folder; parquet + Postgres are the durable audit copy, so the raw CSVs are prunable.
- `make stats` — prints ingest summary stats.

## Ad-hoc charts without the database (DuckDB)

With the stack **down**, `scripts/duckdb_parquet.py` opens the pg_parquet exports directly
via DuckDB's `read_parquet` — no PostgreSQL, no containers:

```bash
uv run python scripts/duckdb_parquet.py                    # example aggregations
uv run python scripts/duckdb_parquet.py --sql "SELECT model, count(*) FROM read_parquet('data/parquet/api_calls.parquet') GROUP BY model"
```

Requires `data/parquet/*.parquet` to already exist (`make export-parquet`, or any prior
`make ingest` run — export happens automatically at the end of the pipeline).

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
written to the CSV unless a price actually changed. Rows for "(for prompts up to/over N
tokens)" are merged into one CSV row carrying the long-context tier. Extra
trailing notes after the tier label, such as "(deprecated)", are ignored.

## Development

```bash
make venv                  # uv sync
make check                 # ruff + mypy --strict + pytest
uv run pytest -m live_db   # opt-in: tests against a running PostgreSQL with sql/*.sql applied
```

`make check` passes with only the default `dev` group (ruff 100 cols, `E,F,I,W,UP,S,B`;
`mypy --strict`; pytest). The `nlp` group is needed only for the `slow` ask tests and the
ask eval/stress commands below. Tests mock nothing away that matters: the report tests shell out to the
real script (skipped if `jq` is missing). The default run excludes `slow` and `live_db`
tests, so it needs no PostgreSQL.

## Optional features (in development)

These features are opt-in. They are not part of the default setup, and they can change.

### Scheduled ingest (macOS / launchd) - Optional

> **Status:** optional, in development. macOS only.

`bin/ingest.sh` is safe to run unattended, but nothing schedules it by default —
`make install-timer` is the explicit, one-time opt-in:

```bash
make install-timer     # installs + loads a launchd agent, runs bin/ingest.sh every 30 min
make uninstall-timer   # unloads + removes it
```

This writes `~/Library/LaunchAgents/com.analyze-claude-sessions.ingest.plist` from
`bin/launchd/com.analyze-claude-sessions.ingest.plist.template`, resolving the repo root and
`docker`'s directory into an explicit `PATH` (launchd jobs start with a minimal environment, no
shell profile). Logs go to `data/logs/ingest.launchd.{out,err}.log`; a completed run also shows
up in `claude.ingest_runs` (`acp query "select * from claude.ingest_runs order by run_id desc
limit 5"`). Requires Docker Desktop (or equivalent) to be running at the scheduled time —
`bin/ingest.sh` starts the `postgres` container itself but not the Docker daemon.

Check/trigger manually:

```bash
launchctl list | grep com.analyze-claude-sessions.ingest
launchctl start com.analyze-claude-sessions.ingest   # run once, right now
```

### Ask in plain English (`acp ask`)

> **Status:** optional, in development. Needs the `nlp` dependency group. Two modes:
> catalog mode (default, offline, no generated SQL) and LLM mode (opt-in `--llm`, calls the
> Anthropic API).

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
match read-only. This is **catalog mode**, the default. It is fully offline — no network
access or API key. It retrieves and runs existing queries; it does not generate SQL. Add `--sql` to see the
matched query, `--no-execute` to only preview it. If nothing matches confidently, it prints
the closest candidate questions instead of guessing.

**Slot-aware matching.** Each catalog row declares the filters its SQL can actually bind
(`session_id`, `since`, `project`, `model`, `limit` — see the `slots` column). Your question
is parsed for the same filters (`session_id=<id>` or a bare UUID; `since 2026-01[-15]`;
`last N days`; `last month`, meaning the previous calendar month, not a trailing 30-day
window; a known project or model name; `top N`). If the best-matching row can't bind a
filter your question carries — e.g. "which model is used least **last month**" matching the
all-time "which model is used least" template — `ask` **refuses** rather than silently running the unfiltered
query, and prints the near-matches instead:

```
$ uv run acp ask "which model is used least last month"
Refusing: your question filters by since ('2026-02-01') but the closest match
"which model is used least" has no such filter. Try one of these instead:
  [0.71] which model is used least
  ...
```

The embedding model (numpy, sentence-transformers) lives in the optional `nlp` dependency
group. The app image ships without it, so `ask` prints an install hint if it is missing;
enable it with a one-time `uv sync --group nlp` (locally) or by rebuilding the image with
that group. Every other `acp` command works without it.

#### Filters

Aggregate rows take optional `since`/`until`/`project`/`model` filters (declared twice in
`slots`, once per `%s`, with `optional: true` and a NULL default = no filter), and "top N"
rows take an optional `limit` (default 10). "last month" binds a closed range: `since` =
first day of the previous month, `until` = first day of this one (exclusive). "last N days"
and "since X" set only `since`. A filter the matched row doesn't declare still refuses.

```bash
uv run acp ask "top 3 sessions by total tokens"
uv run acp ask "tokens per model last month"
uv run acp ask "how much money did I spend"   # cost rows; unpriced calls are counted, not zeroed
```

#### Read-only role

`acp ask` connects with `ASK_DATABASE_URL` (the `ask_reader` role from
`sql/019_ask_reader_role.sql`: SELECT-only, read-only sessions, 15s statement timeout).
Rotate its placeholder password after first init (`ALTER ROLE ask_reader WITH PASSWORD '…'`,
likewise for the owner and grafana roles from `sql/007`), then set `ASK_DATABASE_URL` in `.env`.
Compose publishes postgres on `127.0.0.1` only; exposing it on the LAN is a deliberate override,
so rotate first. If the connected role is a superuser or owns the `claude` schema (e.g. the
variable is unset and `ask` falls back to `DATABASE_URL`), `ask` prints a one-line dim warning
after connecting.

#### LLM mode (`--llm`, RAG, opt-in)

`acp ask --llm "cost per model last week"` retrieves the top 5 catalog examples plus the
`claude.v_schema_catalog` rows they (or your question) mention, and asks Claude to write one
SELECT. The SQL is always printed, checked by `ensure_read_only`, capped with `LIMIT 200`,
then run under `ask_reader`. The generated SQL may read only `claude.*` objects that were shown
to the model (schema block or retrieved examples), never raw upload tables. `--summarize` adds
a short LLM summary and **sends up to 50 result rows to the Anthropic API**;
`--no-execute` stops after printing the SQL. Only the question, catalog examples and schema
leave your machine otherwise.

Needs `uv sync --group llm --group nlp`, `ANTHROPIC_API_KEY`, and an explicit
`ASK_DATABASE_URL` (no owner-role fallback here). Model: `ACP_ASK_LLM_MODEL`
(default `claude-opus-5-5`). Missing key or package is an error, never a silent fallback to
catalog mode. Plain `acp ask` never calls the API.

#### End-to-end ask eval

```bash
make ask-eval                                 # per-kind tables for the tune and test splits
make ask-eval ARGS=--tune                     # sweep threshold x gap margin on tune.yaml only
make ask-eval ARGS=--update-baseline          # rewrite baseline.json from test.yaml
uv run --group nlp pytest -m "slow and live_db" tests/test_nlp_eval.py -s
```

Scores the whole `ask` decision (slots, retrieval, refusal) with `resolve_ask` against the
real `claude.nlp_examples` rows, so run `make up && make init-db` first. Cases live in
`tests/data/ask_eval/`: `tune.yaml` is the only set a threshold may be chosen on, `test.yaml`
is held out and frozen. Each case has a `question`, an `expect` (a catalog question or
`refuse`), a `kind` (`paraphrase`, `near_miss`, `opposite`, `ood`, `typo`, `slot`,
`unbindable`), and optional `expect_params`. The report shows accuracy (with a Wilson 95%
interval), wrong-accept rate, over-refusal rate, and recall@1/@3 per kind. The pytest run
compares the test split to `baseline.json` (accuracy may drop by at most 0.02, wrong-accept
may rise by at most 0.02). A baseline recorded on another model or catalog fails with a
re-baseline message. `--tune` prints a suggested (threshold, gap margin) pair and never edits code. The
`slow` / `live_db` test stays out of `make check`; the fast corpus checks live in
`tests/test_ask_eval.py`.

`tests/data/nlp_eval.yaml` is still read by `scripts/ask_stress.py` and the stress tests
(its `near_miss` section and paraphrase corpus); the ask eval does not use it.

#### Retrieval stack

`acp ask` scores each catalog row as the max over its variant texts (question, the
`paraphrases` array added by `sql/020_nlp_paraphrases.sql`, and the description). The score is
`0.7 * cosine + 0.3 * BM25` over character 3-5-grams, after a catalog-vocabulary spell check
(`sesions` -> `sessions`). A hit is accepted only if the top score is at least
`HYBRID_CONFIDENCE_THRESHOLD` (0.5) and leads the runner-up by `GAP_MARGIN` (0.03); both were
chosen on `tune.yaml` only, with a 10% wrong-accept ceiling. A contrast guard demotes a row whose question says the opposite of
the asked term (least/most, read/write, input/output, cost/tokens).

```bash
uv run acp ask "…" --no-contrast-guard   # skip the opposite-term guard
uv run acp ask "…" --rerank              # opt-in cross-encoder re-rank (downloads a ~90 MB model once)
make ask-eval ARGS="--mode cosine"       # ablation: cosine over variants only
make ask-eval ARGS="--mode hybrid"       # default stack (guard on); add --no-contrast-guard to ablate
make ask-eval ARGS="--tune --max-wrong-accept 0.10"  # looser tune ceiling (default 0.05)
make ask-eval ARGS="--mode rerank --tune"  # sweep the cross-encoder stack
make ask-eval ARGS="--mode hybrid --lexical-weight 0.8 --tune"  # try another cosine/BM25 mix
```

The re-ranker (`cross-encoder/ms-marco-MiniLM-L-6-v2`) is off by default: on `tune.yaml` it
scored worse than the fused scores (it is trained for query-to-passage relevance, not
question-to-question matching). If it cannot load, `ask` falls back to the fused scores with one
warning.

#### ask stress suite (opt-in)

A heavier, real-model stress battery lives in `tests/test_nlp_stress_slow.py` (all tests
marked `slow`) and `scripts/ask_stress.py`. It measures perturbation robustness (typos,
casing, punctuation, filler, truncation, unicode noise, …) against the clean baseline,
sweeps `CONFIDENCE_THRESHOLD` from 0.30–0.75 against the in-catalog/out-of-catalog corpora,
checks retrieval latency at 100/1k/10k rows, and re-runs the `ensure_read_only` adversarial
corpus and slot-extraction fuzzing for regressions. `ensure_read_only` itself parses the
SQL with `pglast` (real PostgreSQL grammar) rather than pattern-matching it, so quoting and
escaping edge cases can't slip past it.

```bash
uv run --group nlp pytest -m slow tests/test_nlp_stress_slow.py -s
uv run --group nlp python scripts/ask_stress.py            # full battery, writes data/reports/
uv run --group nlp python scripts/ask_stress.py --quick    # small/fast sanity run
```

or `make ask-stress` for the full report. It's excluded from `make check` and the default
`pytest` run — same reasons as the eval above — and its reports (`data/reports/`) are
gitignored scratch output, not tracked artifacts.

**Comparing embedding models.** `scripts/ask_stress.py` runs the retrieval part of the
battery for one or more local sentence-transformers models:

```bash
make ask-stress-prefetch                                   # one-time download, one process per model
make ask-stress-compare                                    # --compare: MiniLM, bge-small, mpnet
make ask-stress ASK_MODELS="BAAI/bge-small-en-v1.5"        # extra model(s) via the Makefile
uv run --group nlp python scripts/ask_stress.py -m all-mpnet-base-v2 -m BAAI/bge-small-en-v1.5
```

`--model/-m` is repeatable (order kept, duplicates dropped); with no flags only
`all-MiniLM-L6-v2` runs, as before. Downloads are about 90 MB (MiniLM), 130 MB (bge-small)
and 420 MB (mpnet). A multi-model run refuses to start (exit code 2) if any model is not
cached and `HF_HUB_OFFLINE` is not set yourself, because the backend flips the process
offline after the first cached load; run `make ask-stress-prefetch` first. Models load one
at a time and are released before the next.

The report has a comparison table (one row per model), a per-model near-miss table, then
per-model detail sections; guard, slot-fuzz and fake-backend latency sections run once.
Metrics:

- **Near-miss wrong-accept rate**: share of `near_miss` questions whose top-1 score clears
  the threshold. Nothing in the catalog answers them, so any accept is wrong.
- **Operating point `t*`**: the lowest threshold with out-of-catalog false-accept <= 10%,
  computed exactly from the out-of-catalog top-1 scores (`n_ooc` is shown; with a small set
  it is coarse). It is what you would set as that model's `CONFIDENCE_THRESHOLD`. Reported
  at `t*`: accept rate and near-miss wrong-accept. Near-miss rows never tune `t*`.
- **Separation AUC**: probability that a correct in-catalog top-1 scores above an
  out-of-catalog or near-miss top-1 (ties count 0.5). Threshold-free.
- A fixed 0.5 is not used to rank models: it is tuned for MiniLM and bge scores sit higher.
  The 0.5 numbers are kept, labelled as the current `CONFIDENCE_THRESHOLD`. Multi-model
  runs sweep 0.30-0.95; single-model runs keep 0.30-0.75.
- bge runs **without** its query instruction prefix: that prefix targets short-query to
  long-passage retrieval, while here both sides are short questions, and `CatalogRetriever`
  encodes both sides identically. A query-prefix variant is a possible follow-up.

The CSV now starts with a `model` column (`model, section, key, metric, value`); rows for
the model-independent sections use `model="-"`.

## Upgrading from `analyze-claude-projects`

The project was renamed to `analyze-claude-sessions`. Docker Compose derives volume names from the
project name, so after the rename Compose creates **empty** `analyze-claude-sessions_*` volumes and
the database looks wiped (the old data is still on disk, orphaned). Copy it over once, before
running `make up` on the new version:

```bash
# on the old version
make down
for v in pgdata grafana-data; do
  # The labels mark the volume as Compose-owned; without them every `docker compose` call warns.
  docker volume create --label com.docker.compose.project=analyze-claude-sessions \
    --label com.docker.compose.volume=$v analyze-claude-sessions_$v
  docker run --rm -v analyze-claude-projects_$v:/from -v analyze-claude-sessions_$v:/to \
    alpine cp -a /from/. /to/
done
# optional: skip the slow pg_parquet rebuild
docker tag analyze-claude-projects/postgres-pg-parquet:17-0.5.1 \
  analyze-claude-sessions/postgres-pg-parquet:17-0.5.1
# on the new version
make up
make psql   # select count(*) from claude.api_calls;  must match the old count
```

Only after the counts match, remove the old volumes with `docker volume rm`.

No volume copy wanted? Transcripts are the source of truth: `make init-db && make ingest` rebuilds
everything except the append-only `claude.model_pricing` history (re-run `make pricing-sync-db`).

If you installed the launchd timer, run `make uninstall-timer` before pulling and
`make install-timer` after — the job label changed.

### Fixing the `already exists but was not created by Docker Compose` warning

Volumes copied with an older version of these steps have no Compose labels, so every
`docker compose` call prints that warning. The data is fine. Docker cannot add labels to an
existing volume, so recreate each one through a temporary copy. Back up first (`bin/backup-db.sh`)
and note `select count(*) from claude.api_calls;`.

```bash
docker compose --profile cli --profile dashboard down
# Checksums, not du: directory block allocation differs between copies.
sums() { docker run --rm -v "$1":/d:ro alpine sh -c 'cd /d && find . -type f -exec md5sum {} + | sort -k2'; }
for v in pgdata grafana-data; do
  src=analyze-claude-sessions_$v
  docker volume create tmp_$v
  docker run --rm -v $src:/from -v tmp_$v:/to alpine cp -a /from/. /to/
  [ "$(sums $src | md5sum)" = "$(sums tmp_$v | md5sum)" ] || { echo "mismatch: $v"; break; }
  docker volume rm $src
  docker volume create --label com.docker.compose.project=analyze-claude-sessions \
    --label com.docker.compose.volume=$v $src
  docker run --rm -v tmp_$v:/from -v $src:/to alpine cp -a /from/. /to/
  docker volume rm tmp_$v
done
make up
make psql   # the count must match the one noted before
```
