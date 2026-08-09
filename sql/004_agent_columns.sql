-- Upgrade path for DBs provisioned before the report script grew agent/depth
-- columns and dropped parentUuid. Idempotent so `acp init-db` can replay it
-- safely; a fresh install gets the same shape directly from 002_schema.sql.
--
-- claude.interactions_upload is new audit table: one row per loader run that
-- upserts sessions, capturing how long the run took and how many sessions it
-- touched (new vs. already-existing).

ALTER TABLE claude.api_calls DROP COLUMN IF EXISTS parent_uuid;

ALTER TABLE claude.api_calls
    ADD COLUMN IF NOT EXISTS delta_seconds_global integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS agent                 text,
    ADD COLUMN IF NOT EXISTS agent_type            text,
    ADD COLUMN IF NOT EXISTS depth                 integer NOT NULL DEFAULT 0 CHECK (depth >= 0),
    ADD COLUMN IF NOT EXISTS parent_agent          text,
    ADD COLUMN IF NOT EXISTS model                 text,
    ADD COLUMN IF NOT EXISTS tool_use_id           text,
    ADD COLUMN IF NOT EXISTS description           text;

CREATE INDEX IF NOT EXISTS api_calls_depth_idx ON claude.api_calls (session_id) WHERE depth > 0;
CREATE INDEX IF NOT EXISTS api_calls_model_idx ON claude.api_calls (model);

ALTER TABLE claude.sessions
    ADD COLUMN IF NOT EXISTS has_subagent boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS first_model  text;

CREATE TABLE IF NOT EXISTS claude.interactions_upload (
    upload_id                bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id                   bigint      REFERENCES claude.ingest_runs (run_id) ON DELETE SET NULL,
    "timestamp"               timestamptz NOT NULL DEFAULT now(),
    time_spent                integer     NOT NULL CHECK (time_spent >= 0),
    number_new_sessions       integer     NOT NULL DEFAULT 0,
    number_updated_sessions   integer     NOT NULL DEFAULT 0
);
