"""Live-DB checks that sub-agent API calls link to their own transcript.

Opt-in (`-m live_db`); needs a running Postgres with sql/*.sql applied.
Everything runs in one transaction that is rolled back at the end.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import polars as pl
import psycopg
import pytest
from psycopg import Connection
from psycopg.rows import dict_row

from analyze_claude_sessions.config import Settings
from analyze_claude_sessions.loader import load_api_calls, relink_subagent_calls

pytestmark = pytest.mark.live_db

_COLUMNS: dict[str, Any] = {
    "call_index": pl.Int64,
    "called_at": pl.Datetime(time_zone="UTC"),
    "message_uuid": pl.Utf8,
    "input_tokens": pl.Int64,
    "cache_write_tokens": pl.Int64,
    "cache_read_tokens": pl.Int64,
    "output_tokens": pl.Int64,
    "delta_seconds": pl.Int64,
    "delta_seconds_global": pl.Int64,
    "agent": pl.Utf8,
    "agent_type": pl.Utf8,
    "depth": pl.Int64,
    "parent_agent": pl.Utf8,
    "model": pl.Utf8,
    "tool_use_id": pl.Utf8,
    "description": pl.Utf8,
    "usage_records": pl.Int64,
    "output_tokens_raw": pl.Int64,
}


@pytest.fixture
def conn() -> Iterator[Connection[Any]]:
    settings = Settings.from_env()
    try:
        connection = psycopg.connect(settings.database_url, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        pytest.skip(f"no live database reachable: {exc}")
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _frame() -> pl.DataFrame:
    now = datetime.now(UTC)
    rows = [
        (1, "main"),
        (2, "main"),
        (3, "abc"),
    ]
    return pl.DataFrame(
        {
            "call_index": [i for i, _ in rows],
            "called_at": [now] * 3,
            "message_uuid": [str(uuid.uuid4()) for _ in rows],
            "input_tokens": [1] * 3,
            "cache_write_tokens": [0] * 3,
            "cache_read_tokens": [0] * 3,
            "output_tokens": [1] * 3,
            "delta_seconds": [0] * 3,
            "delta_seconds_global": [0] * 3,
            "agent": [a for _, a in rows],
            "agent_type": [None] * 3,
            "depth": [0, 0, 1],
            "parent_agent": [None] * 3,
            "model": ["m"] * 3,
            "tool_use_id": [None] * 3,
            "description": [None] * 3,
            "usage_records": [1] * 3,
            "output_tokens_raw": [1] * 3,
        },
        schema=_COLUMNS,
    )


def _seed(conn: Connection[Any]) -> tuple[uuid.UUID, int, int]:
    """Insert project, session, main transcript; return (session, main_id, csv_id placeholder)."""
    sid = uuid.uuid4()
    project_id = conn.execute(
        "INSERT INTO claude.projects (dir_name) VALUES (%s) RETURNING project_id",
        (f"-relink-test-{sid}",),
    ).fetchone()["project_id"]  # type: ignore[index]
    conn.execute(
        "INSERT INTO claude.sessions (session_id, project_id) VALUES (%s, %s)", (sid, project_id)
    )
    main_id = _transcript(conn, sid, "")
    return sid, main_id, 0


def _transcript(conn: Connection[Any], sid: uuid.UUID, agent_id: str) -> int:
    row = conn.execute(
        "INSERT INTO claude.transcripts (session_id, agent_id, jsonl_path) "
        "VALUES (%s, %s, %s) RETURNING transcript_id",
        (sid, agent_id, f"relink-test/{sid}/{agent_id}.jsonl"),
    ).fetchone()
    assert row is not None
    return int(row["transcript_id"])


def _owner(conn: Connection[Any], sid: uuid.UUID, agent: str) -> int:
    row = conn.execute(
        "SELECT transcript_id FROM claude.api_calls WHERE session_id = %s AND agent = %s",
        (sid, agent),
    ).fetchone()
    assert row is not None
    return int(row["transcript_id"])


def _load(conn: Connection[Any], sid: uuid.UUID, main_id: int, frame: pl.DataFrame) -> None:
    load_api_calls(conn, transcript_id=main_id, session_id=sid, csv_id=None, frame=frame)


def _count(conn: Connection[Any], sid: uuid.UUID) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM claude.api_calls WHERE session_id = %s", (sid,)
    ).fetchone()
    assert row is not None
    return int(row["n"])


def test_load_links_subagent_row_to_subagent_transcript(conn: Connection[Any]) -> None:
    sid, main_id, _ = _seed(conn)
    sub_id = _transcript(conn, sid, "agent-abc")
    _load(conn, sid, main_id, _frame())
    assert _owner(conn, sid, "abc") == sub_id
    assert _owner(conn, sid, "main") == main_id


def test_relink_fixes_wrong_ingest_order_and_is_idempotent(conn: Connection[Any]) -> None:
    sid, main_id, _ = _seed(conn)
    frame = _frame()
    _load(conn, sid, main_id, frame)  # sub-agent transcript does not exist yet
    assert _owner(conn, sid, "abc") == main_id

    sub_id = _transcript(conn, sid, "agent-abc")
    assert relink_subagent_calls(conn) >= 1  # global: may also backfill real rows
    assert _owner(conn, sid, "abc") == sub_id
    assert relink_subagent_calls(conn) == 0

    conn.execute("DROP TABLE tmp_api_calls")  # staging table lives until commit
    _load(conn, sid, main_id, frame)  # re-load must not duplicate the relinked row
    assert _count(conn, sid) == 3
