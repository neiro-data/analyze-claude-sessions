"""End-to-end ingestion: transcript -> report CSV -> PostgreSQL."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from psycopg import Connection

from .config import Settings
from .discovery import TranscriptFile
from .jsonl_meta import extract_metadata
from .loader import (
    load_api_calls,
    read_report_csv,
    record_csv_file,
    refresh_rollups,
    upsert_project,
    upsert_session,
    upsert_transcript,
)
from .report import generate_report

ProgressHook = Callable[[str], None]


@dataclass(slots=True)
class IngestSummary:
    discovered: int = 0
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    rows_loaded: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)


def _already_current(conn: Connection[Any], transcript: TranscriptFile, sha256: str) -> bool:
    """True when this file is byte-identical to the one already loaded."""
    row = conn.execute(
        """
        SELECT t.jsonl_sha256, count(c.call_id) AS calls
          FROM claude.transcripts t
          LEFT JOIN claude.api_calls c USING (transcript_id)
         WHERE t.session_id = %s AND t.agent_id = %s
         GROUP BY t.jsonl_sha256
        """,
        (str(transcript.session_id), transcript.agent_id),
    ).fetchone()
    return bool(row and row["jsonl_sha256"] == sha256 and row["calls"] > 0)


def ingest_transcripts(
    conn: Connection[Any],
    settings: Settings,
    transcripts: Iterable[TranscriptFile],
    *,
    run_id: int | None = None,
    force: bool = False,
    on_progress: ProgressHook | None = None,
) -> IngestSummary:
    """Report + load each transcript, committing one transaction per file.

    A failure on one file is recorded and the run continues; the caller decides
    whether a partially failed run counts as success.
    """
    summary = IngestSummary()
    settings.ensure_dirs()

    for transcript in transcripts:
        summary.discovered += 1
        label = transcript.label
        try:
            # A subagent entry can be a symlink whose target is gone; that is a
            # missing input, not a pipeline failure.
            if not transcript.path.is_file():
                summary.skipped += 1
                if on_progress:
                    on_progress(f"skip  {label} (unreadable: {transcript.path})")
                continue

            meta = extract_metadata(transcript.path)

            if not force and _already_current(conn, transcript, meta.jsonl_sha256):
                summary.skipped += 1
                if on_progress:
                    on_progress(f"skip  {label} (unchanged)")
                continue

            project_id = upsert_project(conn, transcript.project_dir, meta.cwd)
            upsert_session(conn, transcript.session_id, project_id, meta)
            transcript_id = upsert_transcript(conn, transcript, meta)

            if meta.api_call_count == 0:
                conn.commit()
                summary.processed += 1
                if on_progress:
                    on_progress(f"meta  {label} (no API calls)")
                continue

            report = generate_report(settings.report_script, transcript, settings.csv_dir)
            frame = read_report_csv(report)
            csv_id = record_csv_file(
                conn,
                run_id=run_id,
                transcript_id=transcript_id,
                session_id=transcript.session_id,
                report=report,
                row_count=frame.height,
            )
            loaded = load_api_calls(
                conn,
                transcript_id=transcript_id,
                session_id=transcript.session_id,
                csv_id=csv_id,
                frame=frame,
            )
            conn.commit()

            summary.processed += 1
            summary.rows_loaded += loaded
            if on_progress:
                on_progress(f"load  {label} -> {loaded} calls")
        except Exception as exc:  # noqa: BLE001 - one bad transcript must not stop the run
            conn.rollback()
            summary.failed += 1
            summary.failures.append((label, f"{type(exc).__name__}: {exc}"))
            if on_progress:
                on_progress(f"FAIL  {label}: {exc}")

    refresh_rollups(conn)
    conn.commit()
    return summary
