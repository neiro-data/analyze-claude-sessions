#!/usr/bin/env bash
# Steady-state ingest runner: bring postgres up, ingest new/changed transcripts
# (idempotent), and refresh the parquet exports. Safe to re-run or cron.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

usage() {
  cat <<'EOF'
Usage: bin/ingest.sh [ingest-args...]

Runs: make up -> make ingest ARGS="<ingest-args...>" -> make export-parquet.
Idempotent (make ingest skips unchanged transcripts by sha256), safe to cron.

Examples:
  bin/ingest.sh --project billing
  bin/ingest.sh --force
EOF
}

for arg in "$@"; do
  case "$arg" in
    -h|--help)
      usage
      exit 0
      ;;
  esac
done

make up
make ingest ARGS="$*"
make export-parquet

echo "done: ingest + parquet export complete — run \`make stats\` for headline numbers"
