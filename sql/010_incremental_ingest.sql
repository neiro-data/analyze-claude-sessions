-- Incremental (tail-only) ingest support.
--
-- `acp ingest` previously re-parsed a transcript's whole file whenever it
-- grew, even though the vast majority of its lines were already accounted
-- for. These columns record how much of the file the current aggregates
-- (record_count, first/last_event_at, models, ...) cover, so the loader can
-- seek straight to the appended tail on the next run instead of starting
-- over. See jsonl_meta.TranscriptCheckpoint / extract_metadata.
ALTER TABLE claude.transcripts
    ADD COLUMN IF NOT EXISTS jsonl_prefix_bytes bigint,
    ADD COLUMN IF NOT EXISTS jsonl_prefix_lines  integer;

COMMENT ON COLUMN claude.transcripts.jsonl_prefix_bytes IS
    'Byte length covered by the current aggregates (and by jsonl_sha256, at '
    'the time it was recorded). On the next ingest, if the file has only '
    'grown and its first jsonl_prefix_bytes bytes still hash to jsonl_sha256, '
    'only the appended tail is parsed; otherwise (rewrite/truncation) the '
    'whole file is re-read.';
COMMENT ON COLUMN claude.transcripts.jsonl_prefix_lines IS
    'Physical lines (record_count + malformed_lines) covered by '
    'jsonl_prefix_bytes. Informational only -- resuming seeks by byte offset, '
    'not by line count.';
