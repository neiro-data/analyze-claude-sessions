"""Export relations to Parquet with pg_parquet.

``COPY ... TO`` runs inside the PostgreSQL server, so the destination path is the
one the *container* sees (PARQUET_DIR_IN_DB, bind-mounted to data/parquet).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from psycopg import Connection, sql

# Relations exported by `acp export-parquet` with no arguments.
DEFAULT_RELATIONS: tuple[str, ...] = (
    "claude.api_calls",
    "claude.transcripts",
    "claude.sessions",
    "claude.projects",
    "claude.transcript_record_types",
    "claude.csv_files",
    "claude.ingest_runs",
    "claude.v_transcript_token_summary",
    "claude.v_session_token_summary",
    "claude.v_project_token_summary",
    "claude.v_daily_token_usage",
)

_RELATION = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
_VALID_COMPRESSION = frozenset({"uncompressed", "snappy", "gzip", "brotli", "lz4", "zstd"})


class ExportError(RuntimeError):
    """The relation name or compression codec was rejected."""


@dataclass(frozen=True, slots=True)
class ExportResult:
    relation: str
    uri: str
    row_count: int


def ensure_extension(conn: Connection[Any]) -> str:
    """Create pg_parquet if needed and return its installed version."""
    conn.execute("CREATE EXTENSION IF NOT EXISTS pg_parquet")
    row = conn.execute(
        "SELECT extversion FROM pg_extension WHERE extname = 'pg_parquet'"
    ).fetchone()
    if row is None:
        raise ExportError("pg_parquet is not installed in this database")
    return str(row["extversion"])


def export_relation(
    conn: Connection[Any],
    relation: str,
    *,
    target_dir: str,
    compression: str = "zstd",
    run_id: int | None = None,
) -> ExportResult:
    """Write *relation* to ``<target_dir>/<table>.parquet`` and log it."""
    if not _RELATION.match(relation):
        raise ExportError(f"Expected a schema-qualified lowercase relation, got {relation!r}")
    if compression not in _VALID_COMPRESSION:
        raise ExportError(f"Unsupported compression {compression!r}")

    schema_name, table_name = relation.split(".")
    identifier = sql.Identifier(schema_name, table_name)
    uri = f"{target_dir.rstrip('/')}/{table_name}.parquet"

    count_row = conn.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(identifier)).fetchone()
    row_count = int(count_row["n"]) if count_row else 0

    # The SELECT form rather than `COPY <relation> TO`, which PostgreSQL rejects
    # for views.
    conn.execute(
        sql.SQL("COPY (SELECT * FROM {}) TO {} WITH (format 'parquet', compression {})").format(
            identifier, sql.Literal(uri), sql.Literal(compression)
        )
    )
    conn.execute(
        """
        INSERT INTO claude.parquet_exports (run_id, source_relation, uri, compression, row_count)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (run_id, relation, uri, compression, row_count),
    )
    return ExportResult(relation=relation, uri=uri, row_count=row_count)
