#!/usr/bin/env bash
# Install the launchd job that runs `bin/ingest.sh` on a schedule (darwin only).
#
# NOT invoked automatically by anything -- `make install-timer` is the
# explicit, one-time opt-in a human runs deliberately.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "error: launchd scheduling is darwin-only (this is $(uname -s))." >&2
  exit 1
fi

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

label="com.analyze-claude-projects.ingest"
plist_dir="$HOME/Library/LaunchAgents"
plist_path="$plist_dir/$label.plist"
template="$repo_root/bin/launchd/$label.plist.template"

docker_bin_dir="$(dirname "$(command -v docker)")" || {
  echo "error: 'docker' not found on PATH -- install/start Docker before scheduling." >&2
  exit 1
}

mkdir -p "$plist_dir" "$repo_root/data/logs"

sed \
  -e "s|__REPO_ROOT__|$repo_root|g" \
  -e "s|__DOCKER_BIN_DIR__|$docker_bin_dir|g" \
  -e "s|__HOME__|$HOME|g" \
  "$template" > "$plist_path"

# Idempotent: unload first in case a prior version is already loaded.
launchctl unload "$plist_path" >/dev/null 2>&1 || true
launchctl load "$plist_path"

echo "installed: $plist_path"
echo "runs bin/ingest.sh every 30 minutes; logs under data/logs/ingest.launchd.{out,err}.log"
echo "check status:  launchctl list | grep $label"
echo "run right now: launchctl start $label"
echo "uninstall:     make uninstall-timer"
