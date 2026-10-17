"""Live-DB check that apply_migrations prunes rows for removed sql files.

Opt-in (`-m live_db`); needs a running Postgres. apply_migrations commits, so this
cannot be rolled back; it is idempotent and ends in the desired state.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import Connection
from psycopg.rows import dict_row

from analyze_claude_sessions.config import Settings
from analyze_claude_sessions.db import apply_migrations

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


@pytest.mark.live_db
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


@pytest.mark.live_db
def test_apply_migrations_records_current_files_and_is_stable(conn: Connection[Any]) -> None:
    expected = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in _SQL_DIR.glob("*.sql")}

    apply_migrations(conn, _SQL_DIR)
    first = conn.execute("SELECT filename, sha256 FROM claude.migrations").fetchall()
    apply_migrations(conn, _SQL_DIR)
    second = conn.execute("SELECT filename, sha256 FROM claude.migrations").fetchall()

    assert {r["filename"]: r["sha256"] for r in first} == expected
    assert {r["filename"]: r["sha256"] for r in second} == expected
    assert len(second) == len(expected)


class _FakeCursor:
    def executemany(self, *_: Any) -> None:
        pass


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.committed = False

    def execute(self, query: str, params: Any = None) -> None:
        self.calls.append((query, params))

    def cursor(self) -> _FakeCursor:
        return _FakeCursor()

    def commit(self) -> None:
        self.committed = True


def test_apply_migrations_delete_receives_full_filename_list(tmp_path: Path) -> None:
    (tmp_path / "002_b.sql").write_text("select 2;")
    (tmp_path / "001_a.sql").write_text("select 1;")
    fake = _FakeConn()

    applied = apply_migrations(fake, tmp_path)  # type: ignore[arg-type]

    query, params = fake.calls[-1]
    assert applied == ["001_a.sql", "002_b.sql"]
    assert "DELETE FROM claude.migrations" in query
    assert params == (["001_a.sql", "002_b.sql"],)
    assert fake.committed
