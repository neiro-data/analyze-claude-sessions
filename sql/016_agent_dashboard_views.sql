-- Session-grain "is this session too small to be interesting" flag, backing
-- the Usage overview dashboard's "Small sessions" toggle.
--
-- Idempotent (CREATE OR REPLACE) so `acp init-db` can replay it safely.
-- Deliberately independent of claude.v_session_token_summary: that view is
-- DROP ... CASCADE'd and redefined by both 003 and 008, so depending on it
-- here would break replay ordering.
CREATE OR REPLACE VIEW claude.v_session_size_flags AS
SELECT s.session_id,
       s.duration_seconds,
       count(c.call_id)                                    AS api_calls,
       (coalesce(s.duration_seconds, 0) < 30
        OR count(c.call_id) < 5)                            AS is_small
FROM claude.sessions s
LEFT JOIN claude.transcripts t USING (session_id)
LEFT JOIN claude.api_calls  c USING (transcript_id)
GROUP BY s.session_id, s.duration_seconds;

COMMENT ON VIEW claude.v_session_size_flags IS
    'Per-session small-session flag for the Usage overview dashboard: a session '
    'is_small when it lasts under 30 seconds or makes fewer than 5 API calls.';

-- The grafana_reader default privileges from 007 already cover views created by
-- the owner role, but replaying this file against a DB provisioned before 007
-- would otherwise leave the new view unreadable.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_reader') THEN
        GRANT SELECT ON claude.v_session_size_flags TO grafana_reader;
    END IF;
END
$$;
