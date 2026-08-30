#!/usr/bin/env bash
set -euo pipefail

# Per-API-call breakdown with elapsed time between consecutive calls.
# cache_write/cache_read/input are identical across a requestId group → take .[0]
# timestamp is taken from the first record of each requestId group (call start).
#
# SUMMARY: Processes Claude API call logs (JSONL) to extract per-call metrics:
#   - Token usage (input, cache_write, cache_read, output)
#   - Call timestamps and model
#   - Elapsed time between consecutive calls (per agent and across the whole session)
#   - Which agent made the call (main agent vs. each sub-agent)
# Output: CSV file.
#
# SUB-AGENTS: Claude Code does NOT write sub-agent API calls into the main
# transcript. Each sub-agent gets its own transcript in a sibling directory:
#
#   <dir>/<session-id>.jsonl                            ← main agent
#   <dir>/<session-id>/subagents/agent-<id>.jsonl       ← one file per sub-agent
#   <dir>/<session-id>/subagents/agent-<id>.meta.json   ← agentType, description,
#                                                         toolUseId, parentAgentId,
#                                                         spawnDepth
#
# This script auto-discovers that directory and merges every transcript into one
# timeline sorted by timestamp. Nested sub-agents (spawnDepth >= 2) live in the
# same flat directory and are linked back via parentAgentId.
#
# CAVEAT: a large cache_write in the main transcript is NOT a reliable sub-agent
# marker — a model switch (e.g. haiku → sonnet) also invalidates the cache and
# forces a full re-write. Use the `agent` column, not token jumps.
#
# NOTE: sub-agents run concurrently with the main agent, so delta_seconds_global
# is wall-clock gap between calls anywhere in the session, not idle time.
# delta_seconds is the gap since that same agent's previous call.
#
# Vendored from REPOS/scripts/cache_report_per_call_timing.sh with two fixes:
#   1. derives the filename timestamp with sed instead of BSD-only `date -j -f`,
#      so it behaves identically on macOS and inside the Linux container.
#   2. reads the first timestamp lazily instead of `jq ... | head -1`, which under
#      `set -o pipefail` aborts the script with SIGPIPE (exit 141) on large files.

# arguments

#- $1 = just the jsonl filename or full path to jsonl file
#- $2 = optional directory where input jsonl lives (defaults to .) - only needed if $1 is just filename
#- $3 = optional output directory (defaults to current directory)
#- --main-only = ignore sub-agent transcripts (previous behaviour)

main_only=0
positional=()
for arg in "$@"; do
  case "$arg" in
    --main-only) main_only=1 ;;
    *) positional+=("$arg") ;;
  esac
done
set -- ${positional[@]+"${positional[@]}"}

filename="${1:?Usage: $0 <file.jsonl> [input_dir] [output_dir] [--main-only]}"
input_dir="${2:-.}"
output_dir="${3:-.}"

# Build full input path
input="${input_dir%/}/${filename##*/}"

# Extract base filename without .jsonl extension
base_filename="${filename##*/}"
base_filename="${base_filename%.jsonl}"

# Sub-agent transcripts live in <input-without-.jsonl>/subagents/
subagent_dir="${input%.jsonl}/subagents"

# Extract first timestamp from JSONL to use in output filename.
# `first(inputs | ...)` short-circuits inside jq, so nothing writes into a closed
# pipe the way `jq ... | head -1` does.
first_timestamp=$(jq -rn 'first(inputs | .timestamp // empty)' "$input")
if [[ -z "$first_timestamp" ]]; then
  # Fallback to current timestamp if no records found
  date_stamp=$(date -u +"%Y%m%dT%H:%M:%S")
else
  # 2026-08-08T11:49:35.123Z -> 20260808T11:49:35 (portable, no date(1) parsing)
  date_stamp=$(printf '%s' "$first_timestamp" \
    | sed -E 's/\.[0-9]+//; s/(Z|[+-][0-9]{2}:?[0-9]{2})$//; s/^([0-9]{4})-([0-9]{2})-([0-9]{2})T/\1\2\3T/')
fi

# Construct output CSV filename: base_filename_YYYYMMDDTHH:MM:SS.csv in output_dir
output="${output_dir%/}/${base_filename}_${date_stamp}.csv"

# jq program: one row per usage-bearing record, tagged with agent identity.
# Agent metadata is injected from outside because it lives in the sibling
# .meta.json, not in the transcript records themselves.
extract_prog='
  select(.message.usage and .requestId)
  | {
      rid:         .requestId,
      ts:          .timestamp,
      uuid:        (.uuid // ""),
      model:       (.message.model // ""),
      input:       (.message.usage.input_tokens // 0),
      write:       (.message.usage.cache_creation_input_tokens // 0),
      read:        (.message.usage.cache_read_input_tokens // 0),
      output:      (.message.usage.output_tokens // 0),
      agent:       $agent,
      agent_type:  $atype,
      depth:       ($depth | tonumber),
      parent:      $parent,
      tool_use_id: $tuid,
      description: $desc
    }
'

# Collect tagged rows from the main transcript plus every sub-agent transcript.
collect_rows() {
  jq -c --arg agent "main" --arg atype "main" --arg depth "0" \
        --arg parent "" --arg tuid "" --arg desc "" \
        "$extract_prog" "$input"

  [[ "$main_only" -eq 1 || ! -d "$subagent_dir" ]] && return 0

  for sub in "$subagent_dir"/*.jsonl; do
    [[ -e "$sub" ]] || continue
    agent_id="${sub##*/}"
    agent_id="${agent_id%.jsonl}"
    agent_id="${agent_id#agent-}"

    meta="${sub%.jsonl}.meta.json"
    if [[ -f "$meta" ]]; then
      atype=$(jq -r '.agentType   // ""' "$meta")
      desc=$( jq -r '.description // ""' "$meta")
      tuid=$( jq -r '.toolUseId   // ""' "$meta")
      parent=$(jq -r '.parentAgentId // "main"' "$meta")
      depth=$( jq -r '.spawnDepth  // 1'  "$meta")
    else
      atype=""; desc=""; tuid=""; parent="main"; depth=1
    fi

    jq -c --arg agent "$agent_id" --arg atype "$atype" --arg depth "$depth" \
          --arg parent "$parent" --arg tuid "$tuid" --arg desc "$desc" \
          "$extract_prog" "$sub"
  done
}

# Group by requestId, order the merged timeline, compute both deltas.
process_data() {
  collect_rows | jq -s '
    group_by(.rid)
    | map(.[0] + {output: ([.[].output] | max)})
    | map(.ts_epoch = ((.ts | sub("\\.[0-9]+Z$"; "Z")) | strptime("%Y-%m-%dT%H:%M:%SZ") | mktime))
    | sort_by(.ts)
    | to_entries
    | map(.value + {call: (.key + 1)})
    | . as $rows
    | reduce range(0; length) as $i
        ({last_seen: {}, prev_ts: null, out: []};
          ($rows[$i]) as $row
          | (.last_seen[$row.agent]) as $prev_ts
          | . + {
              out: (.out + [
                $row
                + {delta_global: (if .prev_ts == null then 0
                                  else $row.ts_epoch - .prev_ts end)}
                + {delta_agent:  (if $prev_ts == null then 0
                                  else $row.ts_epoch - $prev_ts end)}
              ]),
              prev_ts: $row.ts_epoch,
              last_seen: (.last_seen + {($row.agent): $row.ts_epoch})
            }
        )
    | .out
    | map(del(.ts_epoch))
  '
}

rows_json=$(process_data)

{
  echo "call,timestamp,uuid,input_tokens,cache_write_tokens,cache_read_tokens,output_tokens,delta_seconds,delta_seconds_global,agent,agent_type,depth,parent_agent,model,tool_use_id,description"
  echo "$rows_json" | jq -r '.[] | [
    .call, .ts, .uuid, .input, .write, .read, .output,
    .delta_agent, .delta_global,
    .agent, .agent_type, .depth, .parent, .model, .tool_use_id, .description
  ] | @csv'
} > "$output"

echo "CSV output written to: $output" >&2

# Per-agent summary to stderr so the sub-agent cost is visible at a glance.
echo "$rows_json" | jq -r '
  group_by(.agent)
  | map({
      agent: .[0].agent,
      type:  .[0].agent_type,
      depth: .[0].depth,
      calls: length,
      write: ([.[].write] | add),
      read:  ([.[].read]  | add),
      out:   ([.[].output] | add)
    })
  | sort_by(.depth, .agent)
  | (["agent","type","depth","calls","cache_write","cache_read","output"] | @tsv),
    (.[] | [.agent, .type, .depth, .calls, .write, .read, .out] | @tsv),
    ("TOTAL\t\t\t" + ([.[].calls] | add | tostring)
       + "\t" + ([.[].write] | add | tostring)
       + "\t" + ([.[].read]  | add | tostring)
       + "\t" + ([.[].out]   | add | tostring))
' >&2
