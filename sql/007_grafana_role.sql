-- Dedicated read-only role for the Grafana datasource. This is intentionally
-- NOT the `claude` owner role: Grafana only ever needs USAGE + SELECT on the
-- `claude` schema, never DDL/DML.
--
-- SECURITY: raw .sql files under docker-entrypoint-initdb.d cannot read
-- environment variables, so the password below is a placeholder. Rotate it
-- immediately after first init, e.g.:
--   ALTER ROLE grafana_reader WITH PASSWORD '<real-secret>';
-- and wire the real secret into the Grafana datasource provisioning config
-- (out of scope for this migration).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_reader') THEN
        CREATE ROLE grafana_reader
            WITH LOGIN
            PASSWORD 'changeme_grafana'
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA claude TO grafana_reader;

-- Existing tables/views.
GRANT SELECT ON ALL TABLES IN SCHEMA claude TO grafana_reader;

-- Future tables/views created by later migrations (e.g. dashboard views added
-- after this one), so Grafana keeps working without a follow-up GRANT.
ALTER DEFAULT PRIVILEGES IN SCHEMA claude
    GRANT SELECT ON TABLES TO grafana_reader;
