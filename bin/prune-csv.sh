#!/usr/bin/env bash
# data/parquet/ + Postgres already hold every loaded call (durable, queryable
# audit copy); claude.csv_files tracks each CSV's sha256. The raw data/csv/
# tree is a redundant raw-audit trail that only needs to survive long enough
# to debug a bad ingest — so it is safely prunable.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

usage() {
  cat <<'EOF'
Usage: bin/prune-csv.sh [--days N] [--archive-dir DIR] [--delete] [--yes] [-h]

Finds session directories directly under data/csv/ whose newest *.csv file
is older than N days (default 30) and either archives them to a tarball
under --archive-dir (default ./data/backups/csv-archive) or, with --delete,
removes them outright without archiving.

Dry-run by default: prints what WOULD happen and does nothing unless --yes
is also passed.

  --days 0 matches EVERY session dir (nothing is newer than 0 days), i.e. it
  prunes the whole folder — intended for a full archive, not a safe default.

  --days N          age threshold in days (default: 30)
  --archive-dir DIR archive destination (default: ./data/backups/csv-archive)
  --delete          skip the tarball, just rm -rf the matched directories
  --yes             actually perform the action (otherwise dry-run only)
  -h, --help        show this help
EOF
}

DAYS=30
ARCHIVE_DIR="./data/backups/csv-archive"
DELETE=0
APPLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --days)
      [[ $# -ge 2 ]] || { echo "error: --days requires a value" >&2; exit 1; }
      DAYS="$2"
      shift 2
      ;;
    --archive-dir)
      [[ $# -ge 2 ]] || { echo "error: --archive-dir requires a value" >&2; exit 1; }
      ARCHIVE_DIR="$2"
      shift 2
      ;;
    --delete)
      DELETE=1
      shift
      ;;
    --yes)
      APPLY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if ! [[ "$DAYS" =~ ^[0-9]+$ ]]; then
  echo "error: --days must be a non-negative integer, got: $DAYS" >&2
  exit 1
fi

CSV_ROOT="data/csv"
if [[ ! -d "$CSV_ROOT" ]]; then
  echo "no $CSV_ROOT directory found — nothing to prune"
  exit 0
fi

# Portable "newest .csv older than N days" check: rather than compare mtimes
# via `stat` (whose flags differ between BSD/macOS and GNU), we ask `find`
# itself — a directory qualifies only if it has *zero* *.csv files newer
# than N days (i.e. its newest CSV is also older than N days). `find -mtime
# +N` semantics are consistent enough across BSD/GNU for this coarse,
# day-granularity check.
if [[ "$DAYS" -eq 0 ]]; then
  echo "warning: --days 0 matches EVERY session dir (prunes the whole folder)" >&2
fi

candidates=()
while IFS= read -r -d '' dir; do
  csv_count=$(find "$dir" -maxdepth 1 -name '*.csv' -print | wc -l | tr -d ' ')
  [[ "$csv_count" -eq 0 ]] && continue
  recent_count=$(find "$dir" -maxdepth 1 -name '*.csv' -mtime "-${DAYS}" -print | wc -l | tr -d ' ')
  if [[ "$recent_count" -eq 0 ]]; then
    candidates+=("$dir")
  fi
done < <(find "$CSV_ROOT" -mindepth 1 -maxdepth 1 -type d -print0)

if [[ ${#candidates[@]} -eq 0 ]]; then
  echo "no session directories under $CSV_ROOT older than $DAYS day(s) — nothing to do"
  exit 0
fi

action="archive to $ARCHIVE_DIR then remove"
[[ "$DELETE" -eq 1 ]] && action="delete (no archive)"

echo "=== DRY RUN (default) unless --yes is passed ==="
echo "action: $action"
echo "threshold: newest *.csv older than $DAYS day(s)"
echo "matched ${#candidates[@]} session dir(s):"
for dir in "${candidates[@]}"; do
  echo "  $dir"
done

if [[ "$APPLY" -ne 1 ]]; then
  echo
  echo "DRY RUN ONLY — no files were changed. Re-run with --yes to apply."
  exit 0
fi

echo
echo "=== APPLYING ==="

if [[ "$DELETE" -eq 1 ]]; then
  for dir in "${candidates[@]}"; do
    rm -rf "$dir"
    echo "deleted: $dir"
  done
else
  mkdir -p "$ARCHIVE_DIR"
  TS="$(date +%Y%m%dT%H%M%S)"
  tarball="$ARCHIVE_DIR/csv-${TS}.tar.gz"
  basenames=()
  for dir in "${candidates[@]}"; do
    basenames+=("$(basename "$dir")")
  done
  tar czf "$tarball" -C "$CSV_ROOT" "${basenames[@]}"
  # Verify the archive is readable before removing the sources it came from.
  if ! tar tzf "$tarball" >/dev/null 2>&1; then
    echo "error: archive verification failed ($tarball) — sources NOT deleted" >&2
    exit 1
  fi
  echo "archived to: $tarball"
  for dir in "${candidates[@]}"; do
    rm -rf "$dir"
    echo "removed: $dir"
  done
fi

echo "done: ${#candidates[@]} session dir(s) processed"
