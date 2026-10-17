"""Live-DB check that apply_migrations prunes rows for removed sql files.

Opt-in (`-m live_db`); needs a running Postgres. apply_migrations commits, so this
cannot be rolled back; it is idempotent and ends in the desired state.
"""

from __future__ import annotations

import hashlib
import logging
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

    try:
        applied = apply_migrations(conn, _SQL_DIR)

        rows = conn.execute("SELECT filename FROM claude.migrations").fetchall()
        assert {r["filename"] for r in rows} == set(applied)
        assert "999_fake_orphan.sql" not in applied
    finally:
        conn.rollback()
        conn.execute("DELETE FROM claude.migrations WHERE filename = %s", ("999_fake_orphan.sql",))
        conn.commit()


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


class _FakeResult:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, str]]:
        return self._rows


class _FakeConn:
    def __init__(self, pruned: list[str] | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.committed = False
        self._pruned = pruned or []

    def execute(self, query: str, params: Any = None) -> _FakeResult:
        self.calls.append((query, params))
        return _FakeResult([{"filename": n} for n in self._pruned])

    def cursor(self) -> _FakeCursor:
        return _FakeCursor()

    def commit(self) -> None:
        self.committed = True


def _two_files(tmp_path: Path) -> None:
    (tmp_path / "002_b.sql").write_text("select 2;")
    (tmp_path / "001_a.sql").write_text("select 1;")


def test_apply_migrations_delete_receives_full_filename_list(tmp_path: Path) -> None:
    _two_files(tmp_path)
    fake = _FakeConn()

    applied = apply_migrations(fake, tmp_path)  # type: ignore[arg-type]

    query, params = next(c for c in fake.calls if "DELETE FROM" in c[0])
    assert applied == ["001_a.sql", "002_b.sql"]
    assert "DELETE FROM claude.migrations" in query
    assert params == (["001_a.sql", "002_b.sql"],)
    assert fake.committed


def test_apply_migrations_warns_when_rows_pruned(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _two_files(tmp_path)
    fake = _FakeConn(pruned=["900_old.sql", "800_older.sql"])

    with caplog.at_level(logging.WARNING, logger="analyze_claude_sessions.db"):
        apply_migrations(fake, tmp_path)  # type: ignore[arg-type]

    assert "pruned 2 migration rows" in caplog.text
    assert "800_older.sql, 900_old.sql" in caplog.text


def test_apply_migrations_silent_when_nothing_pruned(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _two_files(tmp_path)

    with caplog.at_level(logging.WARNING, logger="analyze_claude_sessions.db"):
        apply_migrations(_FakeConn(), tmp_path)  # type: ignore[arg-type]

    assert "pruned" not in caplog.text
