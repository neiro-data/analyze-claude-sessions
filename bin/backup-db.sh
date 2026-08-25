#!/usr/bin/env bash
# Back up the Postgres database (pg_dump custom format) and any exported
# parquet files to a timestamped directory.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

usage() {
  cat <<'EOF'
Usage: bin/backup-db.sh [DEST_DIR]

Backs up the PostgreSQL database (pg_dump -Fc) and data/parquet/*.parquet
into DEST_DIR/<db>_<timestamp>/ (DEST_DIR defaults to ./data/backups).

Restore hint:
  docker compose exec -T postgres pg_restore -U <user> -d <db> --clean < dump.sqlc
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

DEST_DIR="${1:-./data/backups}"

if [[ -f ./.env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

DB_USER="${POSTGRES_USER:-claude}"
DB_NAME="${POSTGRES_DB:-claude_analytics}"

if ! docker compose exec -T postgres pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
  echo "error: postgres container not running — run \`make up\` first" >&2
  exit 1
fi

TS="$(date +%Y%m%dT%H%M%S)"
target="$DEST_DIR/${DB_NAME}_${TS}"
mkdir -p "$target"

docker compose exec -T postgres pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$target/dump.sqlc"

if [[ ! -s "$target/dump.sqlc" ]]; then
  echo "error: pg_dump produced an empty file" >&2
  exit 1
fi

parquet_note="no parquet files found in data/parquet/"
shopt -s nullglob
parquet_files=(data/parquet/*.parquet)
shopt -u nullglob
if [[ ${#parquet_files[@]} -gt 0 ]]; then
  mkdir -p "$target/parquet"
  cp "${parquet_files[@]}" "$target/parquet/"
  parquet_note="copied ${#parquet_files[@]} parquet file(s)"
else
  echo "note: $parquet_note (continuing)"
fi

{
  echo "database: $DB_NAME"
  echo "timestamp: $TS"
  echo
  echo "dump.sqlc:"
  du -h "$target/dump.sqlc"
  echo
  echo "parquet: $parquet_note"
  if [[ -d "$target/parquet" ]]; then
    du -h "$target"/parquet/*.parquet
  fi
  echo
  echo "restore hint:"
  echo "  docker compose exec -T postgres pg_restore -U $DB_USER -d $DB_NAME --clean < dump.sqlc"
  echo
  echo "dump contents:"
  docker compose exec -T postgres pg_restore -l - < "$target/dump.sqlc" 2>/dev/null \
    || pg_restore -l "$target/dump.sqlc" 2>/dev/null \
    || echo "  (pg_restore -l unavailable; inspect dump.sqlc manually)"
} > "$target/MANIFEST.txt"

echo "backup complete: $(cd "$target" && pwd)"
