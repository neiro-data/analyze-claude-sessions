"""Load report CSVs and transcript metadata into PostgreSQL."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import polars as pl
from psycopg import Connection

from .discovery import TranscriptFile
from .jsonl_meta import SessionMetadata
from .report import CsvReport

# Column order used for both the staging table and the COPY stream.
_STAGING_COLUMNS = (
    "call_index",
    "called_at",
    "message_uuid",
    "input_tokens",
    "cache_write_tokens",
    "cache_read_tokens",
    "output_tokens",
    "delta_seconds",
    "delta_seconds_global",
    "agent",
    "agent_type",
    "depth",
    "parent_agent",
    "model",
    "tool_use_id",
    "description",
)

_REQUIRED_CSV_COLUMNS = frozenset(
    {
        "call",
        "timestamp",
        "uuid",
        "input_tokens",
        "cache_write_tokens",
        "cache_read_tokens",
        "output_tokens",
        "delta_seconds",
        "delta_seconds_global",
        "depth",
        "model",
        "agent_type",
    }
)

_INT_COLUMNS = (
    "input_tokens",
    "cache_write_tokens",
    "cache_read_tokens",
    "output_tokens",
    "delta_seconds",
    "delta_seconds_global",
    "depth",
)

_TEXT_COLUMNS = (
    "agent",
    "agent_type",
    "parent_agent",
    "model",
    "tool_use_id",
    "description",
)


class LoadError(RuntimeError):
    """The CSV did not look like a per-call timing report."""


def read_report_csv(report: CsvReport) -> pl.DataFrame:
    """Read a report CSV into the staging column layout."""
    frame = pl.read_csv(report.path, infer_schema_length=0)
    missing = _REQUIRED_CSV_COLUMNS - set(frame.columns)
    if missing:
        raise LoadError(f"{report.path.name} is missing columns: {sorted(missing)}")

    return frame.select(
        pl.col("call").cast(pl.Int32).alias("call_index"),
        pl.col("timestamp").str.to_datetime(time_unit="us", time_zone="UTC").alias("called_at"),
        _blank_to_null("uuid").alias("message_uuid"),
        *(pl.col(name).cast(pl.Int64).alias(name) for name in _INT_COLUMNS),
        *(_blank_to_null(name).alias(name) for name in _TEXT_COLUMNS),
    ).sort("call_index")


def _blank_to_null(column: str) -> pl.Expr:
    stripped = pl.col(column).str.strip_chars()
    return pl.when(stripped.str.len_chars() == 0).then(None).otherwise(stripped)


# --------------------------------------------------------------------------- #
# Metadata upserts
# --------------------------------------------------------------------------- #
def upsert_project(conn: Connection[Any], dir_name: str, project_path: str | None) -> int:
    row = conn.execute(
        """
        INSERT INTO claude.projects (dir_name, project_path)
        VALUES (%s, %s)
        ON CONFLICT (dir_name) DO UPDATE
           SET project_path = COALESCE(EXCLUDED.project_path, claude.projects.project_path),
               updated_at   = now()
        RETURNING project_id
        """,
        (dir_name, project_path),
    ).fetchone()
    if row is None:  # pragma: no cover - RETURNING always yields a row
        raise LoadError(f"Could not upsert project {dir_name}")
    return int(row["project_id"])


def upsert_session(
    conn: Connection[Any],
    session_id: UUID,
    project_id: int,
    meta: SessionMetadata,
) -> bool:
    """Ensure the parent session row exists.

    Subagent transcripts share their parent's session_id and carry no ai_title, so
    session-level fields are only ever filled in, never cleared.

    Returns True when this call inserted a brand-new session row, False when it
    updated an existing one.
    """
    row = conn.execute(
        """
        INSERT INTO claude.sessions (session_id, project_id, ai_title, cwd)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (session_id) DO UPDATE SET
            project_id = EXCLUDED.project_id,
            ai_title   = COALESCE(EXCLUDED.ai_title, claude.sessions.ai_title),
            cwd        = COALESCE(EXCLUDED.cwd, claude.sessions.cwd),
            updated_at = now()
        RETURNING (xmax = 0) AS inserted
        """,
        (str(session_id), project_id, meta.ai_title, meta.cwd),
    ).fetchone()
    if row is None:  # pragma: no cover - RETURNING always yields a row
        raise LoadError(f"Could not upsert session {session_id}")
    return bool(row["inserted"])


def upsert_transcript(
    conn: Connection[Any],
    transcript: TranscriptFile,
    meta: SessionMetadata,
) -> int:
    row = conn.execute(
        """
        INSERT INTO claude.transcripts (
            session_id, agent_id, jsonl_path, jsonl_bytes, jsonl_mtime, jsonl_sha256,
            record_count, malformed_lines, first_event_at, last_event_at, cwd,
            git_branches, cli_versions, models, is_sidechain, user_message_count,
            assistant_msg_count, api_call_count
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (session_id, agent_id) DO UPDATE SET
            jsonl_path          = EXCLUDED.jsonl_path,
            jsonl_bytes         = EXCLUDED.jsonl_bytes,
            jsonl_mtime         = EXCLUDED.jsonl_mtime,
            jsonl_sha256        = EXCLUDED.jsonl_sha256,
            record_count        = EXCLUDED.record_count,
            malformed_lines     = EXCLUDED.malformed_lines,
            first_event_at      = EXCLUDED.first_event_at,
            last_event_at       = EXCLUDED.last_event_at,
            cwd                 = EXCLUDED.cwd,
            git_branches        = EXCLUDED.git_branches,
            cli_versions        = EXCLUDED.cli_versions,
            models              = EXCLUDED.models,
            is_sidechain        = EXCLUDED.is_sidechain,
            user_message_count  = EXCLUDED.user_message_count,
            assistant_msg_count = EXCLUDED.assistant_msg_count,
            api_call_count      = EXCLUDED.api_call_count,
            updated_at          = now()
        RETURNING transcript_id
        """,
        (
            str(transcript.session_id),
            transcript.agent_id,
            meta.jsonl_path,
            meta.jsonl_bytes,
            meta.jsonl_mtime,
            meta.jsonl_sha256,
            meta.record_count,
            meta.malformed_lines,
            meta.first_event_at,
            meta.last_event_at,
            meta.cwd,
            meta.git_branches,
            meta.cli_versions,
            meta.models,
            meta.is_sidechain,
            meta.user_message_count,
            meta.assistant_msg_count,
            meta.api_call_count,
        ),
    ).fetchone()
    if row is None:  # pragma: no cover
        raise LoadError(f"Could not upsert transcript {transcript.path}")
    transcript_id = int(row["transcript_id"])

    conn.execute(
        "DELETE FROM claude.transcript_record_types WHERE transcript_id = %s",
        (transcript_id,),
    )
    if meta.record_types:
        conn.cursor().executemany(
            """
            INSERT INTO claude.transcript_record_types
                (transcript_id, record_type, record_subtype, record_count)
            VALUES (%s, %s, %s, %s)
            """,
            [
                (transcript_id, record_type, subtype, count)
                for (record_type, subtype), count in sorted(meta.record_types.items())
            ],
        )
    return transcript_id


def record_csv_file(
    conn: Connection[Any],
    *,
    run_id: int | None,
    transcript_id: int,
    session_id: UUID,
    report: CsvReport,
    row_count: int,
) -> int:
    row = conn.execute(
        """
        INSERT INTO claude.csv_files
            (run_id, transcript_id, session_id, csv_path, csv_sha256, csv_bytes,
             generated_at, row_count)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (transcript_id, csv_sha256) DO UPDATE
           SET run_id    = EXCLUDED.run_id,
               csv_path  = EXCLUDED.csv_path,
               row_count = EXCLUDED.row_count,
               loaded_at = now()
        RETURNING csv_id
        """,
        (
            run_id,
            transcript_id,
            str(session_id),
            str(report.path),
            report.sha256,
            report.size_bytes,
            report.generated_at,
            row_count,
        ),
    ).fetchone()
    if row is None:  # pragma: no cover
        raise LoadError(f"Could not record CSV {report.path}")
    return int(row["csv_id"])


# --------------------------------------------------------------------------- #
# Fact load
# --------------------------------------------------------------------------- #
def load_api_calls(
    conn: Connection[Any],
    *,
    transcript_id: int,
    session_id: UUID,
    csv_id: int,
    frame: pl.DataFrame,
) -> int:
    """Stage the frame with COPY, then upsert into claude.api_calls."""
    if frame.is_empty():
        return 0

    conn.execute(
        """
        CREATE TEMP TABLE tmp_api_calls (
            call_index            integer,
            called_at             timestamptz,
            message_uuid          uuid,
            input_tokens          bigint,
            cache_write_tokens    bigint,
            cache_read_tokens     bigint,
            output_tokens         bigint,
            delta_seconds         bigint,
            delta_seconds_global  bigint,
            agent                 text,
            agent_type            text,
            depth                 bigint,
            parent_agent          text,
            model                 text,
            tool_use_id           text,
            description           text
        ) ON COMMIT DROP
        """
    )

    columns = ", ".join(_STAGING_COLUMNS)
    with conn.cursor() as cur, cur.copy(f"COPY tmp_api_calls ({columns}) FROM STDIN") as copy:
        for row in frame.select(_STAGING_COLUMNS).iter_rows():
            copy.write_row(row)

    result = conn.execute(
        """
        INSERT INTO claude.api_calls (
            transcript_id, session_id, csv_id, call_index, called_at, message_uuid,
            input_tokens, cache_write_tokens, cache_read_tokens, output_tokens,
            delta_seconds, delta_seconds_global, agent, agent_type, depth,
            parent_agent, model, tool_use_id, description
        )
        SELECT DISTINCT ON (t.message_uuid)
            %s, %s, %s, t.call_index, t.called_at, t.message_uuid,
            t.input_tokens, t.cache_write_tokens, t.cache_read_tokens, t.output_tokens,
            t.delta_seconds, t.delta_seconds_global, t.agent, t.agent_type, t.depth,
            t.parent_agent, t.model, t.tool_use_id, t.description
        FROM tmp_api_calls t
        WHERE t.message_uuid IS NOT NULL
        ORDER BY t.message_uuid, t.call_index
        ON CONFLICT (transcript_id, message_uuid) DO UPDATE SET
            csv_id                = EXCLUDED.csv_id,
            call_index            = EXCLUDED.call_index,
            called_at             = EXCLUDED.called_at,
            input_tokens          = EXCLUDED.input_tokens,
            cache_write_tokens    = EXCLUDED.cache_write_tokens,
            cache_read_tokens     = EXCLUDED.cache_read_tokens,
            output_tokens         = EXCLUDED.output_tokens,
            delta_seconds         = EXCLUDED.delta_seconds,
            delta_seconds_global  = EXCLUDED.delta_seconds_global,
            agent                 = EXCLUDED.agent,
            agent_type            = EXCLUDED.agent_type,
            depth                 = EXCLUDED.depth,
            parent_agent          = EXCLUDED.parent_agent,
            model                 = EXCLUDED.model,
            tool_use_id           = EXCLUDED.tool_use_id,
            description           = EXCLUDED.description
        """,
        (transcript_id, str(session_id), csv_id),
    )
    return int(result.rowcount)


def refresh_rollups(conn: Connection[Any]) -> None:
    """Recompute the denormalised counters on sessions and projects."""
    conn.execute(
        """
        UPDATE claude.sessions s
           SET transcript_count = agg.transcripts,
               subagent_count   = agg.subagents,
               first_event_at   = agg.first_event_at,
               last_event_at    = agg.last_event_at,
               models           = agg.models,
               cwd              = COALESCE(s.cwd, agg.cwd),
               updated_at       = now()
          FROM (
              SELECT session_id,
                     count(*)                                       AS transcripts,
                     count(*) FILTER (WHERE kind = 'subagent')      AS subagents,
                     min(first_event_at)                            AS first_event_at,
                     max(last_event_at)                             AS last_event_at,
                     max(cwd)                                       AS cwd,
                     coalesce(
                         (SELECT array_agg(DISTINCT m ORDER BY m)
                            FROM claude.transcripts t2, unnest(t2.models) AS m
                           WHERE t2.session_id = t.session_id),
                         '{}') AS models
                FROM claude.transcripts t
               GROUP BY session_id
          ) AS agg
         WHERE agg.session_id = s.session_id
        """
    )
    conn.execute(
        """
        UPDATE claude.projects p
           SET session_count = agg.sessions,
               first_seen_at = agg.first_event_at,
               last_seen_at  = agg.last_event_at,
               updated_at    = now()
          FROM (
              SELECT project_id,
                     count(*)            AS sessions,
                     min(first_event_at) AS first_event_at,
                     max(last_event_at)  AS last_event_at
                FROM claude.sessions
               GROUP BY project_id
          ) AS agg
         WHERE agg.project_id = p.project_id
        """
    )
    conn.execute(
        """
        UPDATE claude.sessions s
           SET has_subagent = agg.has_sub,
               first_model  = agg.first_model,
               updated_at   = now()
          FROM (
              SELECT session_id,
                     bool_or(depth > 0) AS has_sub,
                     (array_agg(model ORDER BY called_at, call_index)
                        FILTER (WHERE model IS NOT NULL))[1] AS first_model
                FROM claude.api_calls
               GROUP BY session_id
          ) AS agg
         WHERE agg.session_id = s.session_id
        """
    )


def record_interactions_upload(
    conn: Connection[Any],
    *,
    run_id: int | None,
    time_spent: int,
    new_sessions: int,
    updated_sessions: int,
) -> int:
    row = conn.execute(
        """
        INSERT INTO claude.interactions_upload
            (run_id, time_spent, number_new_sessions, number_updated_sessions)
        VALUES (%s, %s, %s, %s)
        RETURNING upload_id
        """,
        (run_id, time_spent, new_sessions, updated_sessions),
    ).fetchone()
    if row is None:  # pragma: no cover
        raise LoadError("Could not record interactions upload")
    return int(row["upload_id"])
