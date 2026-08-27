-- Upgrade path for DBs provisioned before session lineage tracking. Idempotent
-- so `acp init-db` can replay it safely; a fresh install gets the same shape
-- directly from 002_schema.sql.
--
-- `parent_session_id` is populated from the "Origin session_id: <uuid>" marker
-- the clear-cache-sonnet-low handoff prepends to a child session's first user
-- prompt (see jsonl_meta.py). It is the claimed origin, unvalidated -- no FK,
-- because transcripts are discovered in arbitrary order and the parent may
-- live under a different project dir, be pruned, or never be ingested at all.

ALTER TABLE claude.sessions
    ADD COLUMN IF NOT EXISTS parent_session_id uuid;

CREATE INDEX IF NOT EXISTS sessions_parent_idx
    ON claude.sessions (parent_session_id) WHERE parent_session_id IS NOT NULL;

COMMENT ON COLUMN claude.sessions.parent_session_id IS
    'Origin session uuid as claimed by the child''s first user prompt (the '
    '"Origin session_id: <uuid>" marker left by the clear-cache-sonnet-low '
    'handoff). Unvalidated and not a foreign key: the parent may live under a '
    'different project dir, be pruned, or never be ingested at all.';

-- ---------------------------------------------------------------------------
-- One row per session, with its lineage resolved.
-- ---------------------------------------------------------------------------
-- Walks *up* the parent chain from every session (rather than down from
-- roots) so a self- or mutual-reference still yields exactly one row per
-- session: the path array stops the walk the moment the next hop would
-- revisit a session already seen, so a cycle just ends the walk early
-- instead of looping.
CREATE OR REPLACE VIEW claude.v_session_lineage AS
WITH RECURSIVE walk AS (
    SELECT
        s.session_id            AS start_session_id,
        s.session_id            AS current_session_id,
        s.parent_session_id     AS next_parent_id,
        0                       AS lineage_depth,
        ARRAY[s.session_id]     AS path
    FROM claude.sessions s

    UNION ALL

    SELECT
        w.start_session_id,
        p.session_id,
        p.parent_session_id,
        w.lineage_depth + 1,
        w.path || p.session_id
    FROM walk w
    JOIN claude.sessions p ON p.session_id = w.next_parent_id
    WHERE w.next_parent_id IS NOT NULL
      AND NOT p.session_id = ANY(w.path)
),
terminal AS (
    SELECT DISTINCT ON (start_session_id)
        start_session_id,
        current_session_id AS root_session_id,
        lineage_depth
    FROM walk
    ORDER BY start_session_id, lineage_depth DESC
)
SELECT
    s.session_id,
    s.parent_session_id,
    (parent.session_id IS NOT NULL)               AS parent_ingested,
    (s.parent_session_id IS NOT NULL)             AS is_child,
    (coalesce(children.child_count, 0) > 0)       AS is_parent,
    coalesce(children.child_count, 0)             AS child_count,
    CASE
        WHEN s.parent_session_id IS NOT NULL THEN 'child'
        WHEN coalesce(children.child_count, 0) > 0 THEN 'parent'
        ELSE 'standalone'
    END                                            AS session_role,
    terminal.root_session_id,
    terminal.lineage_depth
FROM claude.sessions s
LEFT JOIN claude.sessions parent ON parent.session_id = s.parent_session_id
LEFT JOIN (
    SELECT parent_session_id, count(*) AS child_count
      FROM claude.sessions
     WHERE parent_session_id IS NOT NULL
     GROUP BY parent_session_id
) children ON children.parent_session_id = s.session_id
JOIN terminal ON terminal.start_session_id = s.session_id;

COMMENT ON VIEW claude.v_session_lineage IS
    'Per-session lineage: claimed parent, whether it was actually ingested, '
    'direct child count, role (child/parent/standalone), and root/depth from '
    'a cycle-safe recursive walk (a self- or mutual-reference stops instead '
    'of looping).';

-- ---------------------------------------------------------------------------
-- Per-root rollup: did the handoff chain actually save anything end to end?
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW claude.v_session_family AS
SELECT
    l.root_session_id,
    count(*)                                          AS chain_length,
    coalesce(array_agg(DISTINCT m ORDER BY m)
             FILTER (WHERE m IS NOT NULL), '{}')       AS distinct_models,
    coalesce(sum(sts.api_calls), 0)                    AS total_api_calls,
    coalesce(sum(sts.total_input_tokens), 0)           AS total_input_tokens,
    coalesce(sum(sts.output_tokens), 0)                AS total_output_tokens,
    min(sts.first_event_at)                            AS family_first_event_at,
    max(sts.last_event_at)                             AS family_last_event_at,
    EXTRACT(EPOCH FROM (max(sts.last_event_at) - min(sts.first_event_at)))
                                                        AS family_span_seconds
FROM claude.v_session_lineage l
JOIN claude.v_session_token_summary sts USING (session_id)
LEFT JOIN LATERAL unnest(sts.models) AS m ON true
GROUP BY l.root_session_id;

COMMENT ON VIEW claude.v_session_family IS
    'Per-root rollup of a whole parent/child chain: chain length, models used '
    'across the family, total calls/tokens, and the first->last span over '
    'the whole family.';
