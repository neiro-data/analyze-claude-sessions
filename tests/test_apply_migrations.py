"""Live-DB check that apply_migrations prunes rows for removed sql files.

Opt-in (`-m live_db`); needs a running Postgres. apply_migrations commits, so this
cannot be rolled back; it is idempotent and ends in the desired state.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import Connection
from psycopg.rows import dict_row

from analyze_claude_sessions.config import Settings
from analyze_claude_sessions.db import apply_migrations

pytestmark = pytest.mark.live_db

_SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


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


def test_apply_migrations_prunes_orphans(conn: Connection[Any]) -> None:
    apply_migrations(conn, _SQL_DIR)
    conn.execute(
        "INSERT INTO claude.migrations (filename, sha256) VALUES (%s, %s)",
        ("999_fake_orphan.sql", "0" * 64),
    )
    conn.commit()

    applied = apply_migrations(conn, _SQL_DIR)

    rows = conn.execute("SELECT filename FROM claude.migrations").fetchall()
    assert {r["filename"] for r in rows} == set(applied)
    assert "999_fake_orphan.sql" not in applied
