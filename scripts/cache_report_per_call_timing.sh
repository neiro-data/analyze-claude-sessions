#!/usr/bin/env bash
set -euo pipefail

# Per-API-call breakdown with elapsed time between consecutive calls.
# cache_write/cache_read/input are identical across a requestId group -> take .[0]
# timestamp is taken from the first record of each requestId group (call start).
#
# SUMMARY: Processes Claude API call logs (JSONL) to extract per-call metrics:
#   - Token usage (input, cache_write, cache_read, output)
#   - Call timestamps and the assistant/parent message uuids
#   - Elapsed time between consecutive calls
# Output: a CSV file whose name is derived from the JSONL basename and the
#         timestamp of the first record in the file.
#
# Vendored from REPOS/scripts/cache_report_per_call_timing.sh with three fixes:
#   1. emits the parentUuid column (present in the reference CSV, missing upstream)
#   2. derives the filename timestamp with sed instead of BSD-only `date -j -f`,
#      so it behaves identically on macOS and inside the Linux container.
#   3. reads the first timestamp lazily instead of `jq ... | head -1`, which under
#      `set -o pipefail` aborts the script with SIGPIPE (exit 141) on large files.

# arguments

#- $1 = just the jsonl filename or full path to jsonl file
#- $2 = optional directory where input jsonl lives (defaults to .) - only needed if $1 is just filename
#- $3 = optional output directory (defaults to current directory)

filename="${1:?Usage: $0 <file.jsonl> [input_dir] [output_dir]}"
input_dir="${2:-.}"
output_dir="${3:-.}"

# Build full input path
input="${input_dir%/}/${filename##*/}"

# Extract base filename without .jsonl extension
base_filename="${filename##*/}"
base_filename="${base_filename%.jsonl}"

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

# Process JSONL and emit the per-call rows as JSON
process_data() {
  jq -s '
    [.[] | select(.message.usage and .requestId)]
    | group_by(.requestId)
    | to_entries
    | map({
        call: (.key + 1),
        ts: .value[0].timestamp,
        uuid: (.value[0].uuid // ""),
        parent: (.value[0].parentUuid // ""),
        input: (.value[0].message.usage.input_tokens // 0),
        write: (.value[0].message.usage.cache_creation_input_tokens // 0),
        read:  (.value[0].message.usage.cache_read_input_tokens // 0),
        output: ([.value[].message.usage.output_tokens // 0] | max)
      })
  ' "$input" \
  | jq '
    map(.ts_epoch = ((.ts | sub("\\.[0-9]+Z$"; "Z")) | strptime("%Y-%m-%dT%H:%M:%SZ") | mktime))
    | . as $rows
    | [range(0; length) |
        if . == 0 then $rows[0] + {delta_s: 0}
        else $rows[.] + {delta_s: ($rows[.].ts_epoch - $rows[.-1].ts_epoch)}
        end
      ]
    | map(del(.ts_epoch))
  '
}

# Always output to CSV file with auto-generated name
{
  echo "call,timestamp,uuid,parentUuid,input_tokens,cache_write_tokens,cache_read_tokens,output_tokens,delta_seconds"
  process_data | jq -r '.[] | [.call, .ts, .uuid, .parent, .input, .write, .read, .output, .delta_s] | @csv'
} > "$output"

echo "CSV output written to: $output" >&2
