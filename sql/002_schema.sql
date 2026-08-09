-- Core model.
--
--   projects 1---* sessions 1---* transcripts 1---* api_calls
--                                      |                ^
--                                      |                |
--            ingest_runs 1---* csv_files ---------------+
--
-- Grain note: a session owns more than one .jsonl. The main transcript lives at
-- <project>/<session_id>.jsonl, and every subagent it spawned gets its own file at
-- <project>/<session_id>/subagents/agent-<id>.jsonl. Those subagent files carry the
-- parent's sessionId but a disjoint set of requestIds, so they are genuine extra API
-- calls -- hence `transcripts`, not `sessions`, is the unit that maps 1:1 onto a CSV.
--
-- api_calls is the fact table fed by cache_report_per_call_timing.sh; everything
-- else is metadata: where the transcript came from, what it looked like, and which
-- ingestion produced which rows.

-- ---------------------------------------------------------------------------
-- Metadata: one row per directory under ~/.claude/projects
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claude.projects (
    project_id      integer     GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dir_name        text        NOT NULL UNIQUE,
    project_path    text,
    session_count   integer     NOT NULL DEFAULT 0,
    first_seen_at   timestamptz,
    last_seen_at    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

COMMENT ON COLUMN claude.projects.dir_name IS
    'Path-encoded folder name, e.g. -Users-me-Documents-REPOS-foo.';
COMMENT ON COLUMN claude.projects.project_path IS
    'Real working directory, read from the transcript ``cwd`` field (the encoded '
    'dir_name is lossy and cannot be decoded reliably).';

-- ---------------------------------------------------------------------------
-- Metadata: one row per Claude Code session, rolled up from its transcripts
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claude.sessions (
    session_id          uuid        PRIMARY KEY,
    project_id          integer     NOT NULL REFERENCES claude.projects (project_id) ON DELETE CASCADE,
    ai_title            text,
    cwd                 text,
    transcript_count    integer     NOT NULL DEFAULT 0,
    subagent_count      integer     NOT NULL DEFAULT 0,
    first_event_at      timestamptz,
    last_event_at       timestamptz,
    duration_seconds    numeric(14, 3)
        GENERATED ALWAYS AS (EXTRACT(EPOCH FROM (last_event_at - first_event_at))) STORED,
    models              text[]      NOT NULL DEFAULT '{}',
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS sessions_project_idx     ON claude.sessions (project_id);
CREATE INDEX IF NOT EXISTS sessions_first_event_idx ON claude.sessions (first_event_at);

-- ---------------------------------------------------------------------------
-- Metadata: one row per .jsonl file
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claude.transcripts (
    transcript_id       bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id          uuid        NOT NULL REFERENCES claude.sessions (session_id) ON DELETE CASCADE,
    agent_id            text        NOT NULL DEFAULT '',
    kind                text        GENERATED ALWAYS AS
        (CASE WHEN agent_id = '' THEN 'session' ELSE 'subagent' END) STORED,
    jsonl_path          text        NOT NULL,
    jsonl_bytes         bigint,
    jsonl_mtime         timestamptz,
    jsonl_sha256        char(64),
    record_count        integer,
    malformed_lines     integer     NOT NULL DEFAULT 0,
    first_event_at      timestamptz,
    last_event_at       timestamptz,
    duration_seconds    numeric(14, 3)
        GENERATED ALWAYS AS (EXTRACT(EPOCH FROM (last_event_at - first_event_at))) STORED,
    cwd                 text,
    git_branches        text[]      NOT NULL DEFAULT '{}',
    cli_versions        text[]      NOT NULL DEFAULT '{}',
    models              text[]      NOT NULL DEFAULT '{}',
    is_sidechain        boolean     NOT NULL DEFAULT false,
    user_message_count  integer     NOT NULL DEFAULT 0,
    assistant_msg_count integer     NOT NULL DEFAULT 0,
    api_call_count      integer     NOT NULL DEFAULT 0,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, agent_id)
);

CREATE INDEX IF NOT EXISTS transcripts_session_idx ON claude.transcripts (session_id);
CREATE INDEX IF NOT EXISTS transcripts_kind_idx    ON claude.transcripts (kind);

COMMENT ON COLUMN claude.transcripts.agent_id IS
    'Empty string for the main session transcript; otherwise the agent-<id> filename '
    'stem of a subagent transcript.';
COMMENT ON COLUMN claude.transcripts.jsonl_sha256 IS
    'Digest at ingest time; a change means the transcript grew and should be re-reported.';

-- ---------------------------------------------------------------------------
-- Metadata: type/subtype histogram per transcript
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claude.transcript_record_types (
    transcript_id   bigint      NOT NULL REFERENCES claude.transcripts (transcript_id) ON DELETE CASCADE,
    record_type     text        NOT NULL,
    record_subtype  text        NOT NULL DEFAULT '',
    record_count    integer     NOT NULL,
    PRIMARY KEY (transcript_id, record_type, record_subtype)
);

-- ---------------------------------------------------------------------------
-- Audit: one row per `acp` pipeline invocation
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claude.ingest_runs (
    run_id                 bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at             timestamptz NOT NULL DEFAULT now(),
    finished_at            timestamptz,
    status                 text        NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'succeeded', 'failed')),
    command                text        NOT NULL,
    source_root            text        NOT NULL,
    csv_dir                text,
    tool_version           text,
    transcripts_discovered integer     NOT NULL DEFAULT 0,
    transcripts_processed  integer     NOT NULL DEFAULT 0,
    transcripts_skipped    integer     NOT NULL DEFAULT 0,
    transcripts_failed     integer     NOT NULL DEFAULT 0,
    rows_loaded            bigint      NOT NULL DEFAULT 0,
    error_message          text
);

-- ---------------------------------------------------------------------------
-- Audit: one row per generated CSV
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claude.csv_files (
    csv_id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id          bigint      REFERENCES claude.ingest_runs (run_id) ON DELETE SET NULL,
    transcript_id   bigint      NOT NULL REFERENCES claude.transcripts (transcript_id) ON DELETE CASCADE,
    session_id      uuid        NOT NULL REFERENCES claude.sessions (session_id) ON DELETE CASCADE,
    csv_path        text        NOT NULL,
    csv_sha256      char(64)    NOT NULL,
    csv_bytes       bigint,
    generated_at    timestamptz,
    row_count       integer     NOT NULL DEFAULT 0,
    loaded_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (transcript_id, csv_sha256)
);

CREATE INDEX IF NOT EXISTS csv_files_run_idx ON claude.csv_files (run_id);

COMMENT ON COLUMN claude.csv_files.generated_at IS
    'Timestamp encoded in the CSV filename by cache_report_per_call_timing.sh, '
    'i.e. the first record of the transcript.';

-- ---------------------------------------------------------------------------
-- Fact: one row per Claude API call (one requestId group in a transcript)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claude.api_calls (
    call_id             bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transcript_id       bigint      NOT NULL REFERENCES claude.transcripts (transcript_id) ON DELETE CASCADE,
    session_id          uuid        NOT NULL REFERENCES claude.sessions (session_id) ON DELETE CASCADE,
    csv_id              bigint      REFERENCES claude.csv_files (csv_id) ON DELETE SET NULL,
    call_index          integer     NOT NULL CHECK (call_index > 0),
    called_at           timestamptz NOT NULL,
    message_uuid        uuid        NOT NULL,
    parent_uuid         uuid,
    input_tokens        integer     NOT NULL DEFAULT 0 CHECK (input_tokens       >= 0),
    cache_write_tokens  integer     NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    cache_read_tokens   integer     NOT NULL DEFAULT 0 CHECK (cache_read_tokens  >= 0),
    output_tokens       integer     NOT NULL DEFAULT 0 CHECK (output_tokens      >= 0),
    delta_seconds       integer     NOT NULL DEFAULT 0,
    total_input_tokens  integer
        GENERATED ALWAYS AS (input_tokens + cache_write_tokens + cache_read_tokens) STORED,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (transcript_id, message_uuid)
);

CREATE INDEX IF NOT EXISTS api_calls_transcript_idx ON claude.api_calls (transcript_id, call_index);
CREATE INDEX IF NOT EXISTS api_calls_session_idx    ON claude.api_calls (session_id);
CREATE INDEX IF NOT EXISTS api_calls_called_at_idx  ON claude.api_calls (called_at);

COMMENT ON TABLE claude.api_calls IS
    'Columns call_index..delta_seconds map 1:1 onto the CSV produced by '
    'cache_report_per_call_timing.sh.';

-- ---------------------------------------------------------------------------
-- Audit: one row per pg_parquet export
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claude.parquet_exports (
    export_id       bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id          bigint      REFERENCES claude.ingest_runs (run_id) ON DELETE SET NULL,
    source_relation text        NOT NULL,
    uri             text        NOT NULL,
    compression     text        NOT NULL DEFAULT 'zstd',
    row_count       bigint      NOT NULL DEFAULT 0,
    exported_at     timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Applied-migration bookkeeping (used by `acp init-db`)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claude.migrations (
    filename    text        PRIMARY KEY,
    sha256      char(64)    NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
