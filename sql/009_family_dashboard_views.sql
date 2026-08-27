-- Dashboard support views for the combined "Session Family" dashboard, which
-- renders a whole parent/child chain (root + descendants) as one merged
-- session rather than one session at a time.
--
-- Idempotent (CREATE OR REPLACE) so `acp init-db` can replay it safely.
-- Depends on 006_dashboard_views.sql (claude.v_call_detail) and
-- 008_session_lineage.sql (claude.v_session_lineage).

-- ---------------------------------------------------------------------------
-- One row per family member, numbered in the order the sessions actually ran.
-- ---------------------------------------------------------------------------
-- `member_index` is 1..N within a family ordered by first activity, so the
-- dashboard's "exclude the last member" toggle is just
-- `member_index < member_count`. Sessions that were ingested but produced no
-- API call have a NULL first_event_at; they sort last (and never displace a
-- session that did run) but still count as members.
CREATE OR REPLACE VIEW claude.v_session_family_member AS
SELECT
    l.root_session_id,
    l.session_id,
    l.session_role,
    l.parent_session_id,
    l.lineage_depth,
    sts.project_dir,
    sts.ai_title,
    sts.first_event_at,
    sts.last_event_at,
    sts.duration_seconds,
    sts.api_calls,
    sts.total_input_tokens,
    sts.output_tokens,
    (row_number() OVER w)                                   AS member_index,
    (count(*) OVER (PARTITION BY l.root_session_id))        AS member_count,
    ((row_number() OVER w)
        = (count(*) OVER (PARTITION BY l.root_session_id))) AS is_last_member,
    ((row_number() OVER w)::text || '. '
        || coalesce(nullif(sts.ai_title, ''),
                    left(l.session_id::text, 8)))           AS member_label
FROM claude.v_session_lineage l
JOIN claude.v_session_token_summary sts USING (session_id)
WINDOW w AS (
    PARTITION BY l.root_session_id
    ORDER BY sts.first_event_at NULLS LAST, l.session_id
);

COMMENT ON VIEW claude.v_session_family_member IS
    'One row per session in a lineage family, numbered 1..N by first activity '
    '(member_index/member_count/is_last_member) with a display label, so a '
    'dashboard can render the whole chain as one merged session and optionally '
    'drop the most recent member.';

-- ---------------------------------------------------------------------------
-- Every API call in a family, carrying the identity of the member it ran in.
-- ---------------------------------------------------------------------------
-- Deliberately NOT ordered or re-indexed here: the consumer filters members
-- first (e.g. the exclude-last toggle) and only then computes a merged
-- position with `row_number() OVER (ORDER BY called_at)`, so the index stays
-- contiguous under either toggle state.
CREATE OR REPLACE VIEW claude.v_family_call_detail AS
SELECT
    m.root_session_id,
    m.member_index,
    m.member_count,
    m.member_label,
    m.is_last_member,
    d.session_id,
    d.transcript_id,
    d.kind,
    d.call_id,
    d.call_index,
    d.called_at,
    d.delta_seconds,
    d.agent,
    d.agent_type,
    d.depth,
    d.parent_agent,
    d.model,
    d.input_tokens,
    d.cache_write_tokens,
    d.cache_read_tokens,
    d.output_tokens,
    d.total_input_tokens
FROM claude.v_session_family_member m
JOIN claude.v_call_detail d USING (session_id);

COMMENT ON VIEW claude.v_family_call_detail IS
    'Row-grain API calls for a whole lineage family, each tagged with its '
    'member_index/member_label so calls from several sessions can be merged '
    'into one timestamp-ordered timeline with a colour per member.';

-- The grafana_reader default privileges from 007 already cover views created
-- by the owner role, but replaying this file against a DB provisioned before
-- 007 would otherwise leave the new views unreadable.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_reader') THEN
        GRANT SELECT ON claude.v_session_family_member TO grafana_reader;
        GRANT SELECT ON claude.v_family_call_detail    TO grafana_reader;
    END IF;
END
$$;
