#!/usr/bin/env bash
# Unload and remove the launchd job installed by bin/launchd/install.sh.
set -euo pipefail

label="com.analyze-claude-projects.ingest"
plist_path="$HOME/Library/LaunchAgents/$label.plist"

if [[ ! -f "$plist_path" ]]; then
  echo "not installed: $plist_path (nothing to do)"
  exit 0
fi

launchctl unload "$plist_path" >/dev/null 2>&1 || true
rm -f "$plist_path"

echo "uninstalled: $plist_path"
