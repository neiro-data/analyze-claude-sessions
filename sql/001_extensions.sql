-- Extensions and schema namespace.
-- Runs from /docker-entrypoint-initdb.d on a fresh volume, and is re-applied
-- idempotently by `acp init-db`.

CREATE EXTENSION IF NOT EXISTS pg_parquet;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS claude;

COMMENT ON SCHEMA claude IS
    'Analytics over Claude Code session transcripts (~/.claude/projects/**/*.jsonl).';
