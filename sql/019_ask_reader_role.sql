-- Dedicated read-only role for `acp ask` (ASK_DATABASE_URL). Same shape as
-- sql/007_grafana_role.sql: SELECT-only on the `claude` schema, never DDL/DML,
-- so a catalog row or a bug in the guard cannot write even if it slips past
-- `ensure_read_only`.
--
-- SECURITY: raw .sql files cannot read environment variables, so the password
-- below is a placeholder. Rotate it immediately after first init, e.g.:
--   ALTER ROLE ask_reader WITH PASSWORD '<real-secret>';
-- and put the real secret in ASK_DATABASE_URL.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ask_reader') THEN
        CREATE ROLE ask_reader
            WITH LOGIN
            PASSWORD 'changeme_ask'
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA claude TO ask_reader;

-- Existing tables/views.
GRANT SELECT ON ALL TABLES IN SCHEMA claude TO ask_reader;

-- Future tables/views created by later migrations.
ALTER DEFAULT PRIVILEGES IN SCHEMA claude
    GRANT SELECT ON TABLES TO ask_reader;

-- Belt and braces: every session of this role starts read-only with a timeout.
ALTER ROLE ask_reader SET default_transaction_read_only = on;
ALTER ROLE ask_reader SET statement_timeout = '15s';
