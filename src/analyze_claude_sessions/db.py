"""PostgreSQL access: connections, migrations, and the ingest-run audit trail."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row


@contextmanager
def connect(database_url: str, *, autocommit: bool = False) -> Iterator[Connection[Any]]:
    """Open a connection with dict rows; commits on clean exit, rolls back on error."""
    with psycopg.connect(database_url, autocommit=autocommit, row_factory=dict_row) as conn:
        yield conn


def apply_migrations(conn: Connection[Any], sql_dir: Path) -> list[str]:
    """Run every ``*.sql`` in *sql_dir* in filename order, recording what was applied.

    The scripts are written to be idempotent, so re-running them is safe; the
    claude.migrations table exists to show *when* each one last changed.
    """
    files = sorted(sql_dir.glob("*.sql"))
    if not files:
        raise FileNotFoundError(f"No .sql files in {sql_dir}")

    digests: list[tuple[str, str]] = []
    for path in files:
        body = path.read_text(encoding="utf-8")
        conn.execute(body)
        digests.append((path.name, hashlib.sha256(body.encode("utf-8")).hexdigest()))

    # Recorded only after every file ran, since claude.migrations is itself created
    # by one of them.
    conn.cursor().executemany(
        """
        INSERT INTO claude.migrations (filename, sha256)
        VALUES (%s, %s)
        ON CONFLICT (filename)
        DO UPDATE SET sha256 = EXCLUDED.sha256, applied_at = now()
        """,
        digests,
    )
    conn.commit()
    return [name for name, _ in digests]


def start_run(conn: Connection[Any], *, command: str, source_root: str, csv_dir: str) -> int:
    """Open an ingest_runs row and return its id."""
    from . import __version__

    row = conn.execute(
        """
        INSERT INTO claude.ingest_runs (command, source_root, csv_dir, tool_version)
        VALUES (%s, %s, %s, %s)
        RETURNING run_id
        """,
        (command, source_root, csv_dir, __version__),
    ).fetchone()
    assert row is not None  # noqa: S101 - RETURNING always yields a row
    conn.commit()
    return int(row["run_id"])


def finish_run(
    conn: Connection[Any],
    run_id: int,
    *,
    status: str,
    discovered: int = 0,
    processed: int = 0,
    skipped: int = 0,
    failed: int = 0,
    rows_loaded: int = 0,
    error_message: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE claude.ingest_runs
           SET finished_at            = now(),
               status                 = %s,
               transcripts_discovered = %s,
               transcripts_processed  = %s,
               transcripts_skipped    = %s,
               transcripts_failed     = %s,
               rows_loaded            = %s,
               error_message          = %s
         WHERE run_id = %s
        """,
        (status, discovered, processed, skipped, failed, rows_loaded, error_message, run_id),
    )
    conn.commit()


def fetch_all(
    conn: Connection[Any], sql: str, params: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]
