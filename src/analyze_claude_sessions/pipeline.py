"""End-to-end ingestion: transcript -> report CSV -> PostgreSQL."""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from psycopg import Connection

from .config import Settings
from .discovery import TranscriptFile
from .jsonl_meta import TranscriptCheckpoint, extract_metadata, hash_file
from .loader import (
    load_api_calls,
    read_report_csv,
    record_csv_file,
    record_interactions_upload,
    refresh_rollups,
    relink_subagent_calls,
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


def _prior_owner(conn: Connection[Any], jsonl_sha256: str) -> tuple[str, str] | None:
    """The ``(session_id, agent_id)`` already recorded in the DB for this content, if any."""
    row = conn.execute(
        """
        SELECT session_id, agent_id
          FROM claude.transcripts
         WHERE jsonl_sha256 = %s AND agent_id <> ''
         LIMIT 1
        """,
        (jsonl_sha256,),
    ).fetchone()
    if row is None:
        return None
    return str(row["session_id"]), str(row["agent_id"])


def _reconcile_stale_owner(conn: Connection[Any], jsonl_sha256: str, owner: TranscriptFile) -> None:
    """Delete any other transcript row already holding this content's hash.

    Ownership of a duplicate-content subagent group is pinned by
    ``jsonl_sha256`` (finding 1): once *owner* is chosen for this hash, any
    other ``claude.transcripts`` row that previously claimed the same hash is
    stale and must be removed, or its ``api_calls``/``transcript_record_types``
    would double-count alongside the newly (re)assigned owner. ``ON DELETE
    CASCADE`` on both child tables makes this a single, cheap delete.
    """
    conn.execute(
        """
        DELETE FROM claude.transcripts
         WHERE jsonl_sha256 = %s AND agent_id <> ''
           AND NOT (session_id = %s AND agent_id = %s)
        """,
        (jsonl_sha256, str(owner.session_id), owner.agent_id),
    )


def _dedupe_subagents(
    conn: Connection[Any],
    transcripts: list[TranscriptFile],
) -> tuple[
    list[TranscriptFile],
    list[tuple[TranscriptFile, TranscriptFile, bool]],
    dict[Path, str],
]:
    """Attribute a symlinked subagent transcript to exactly one owning session.

    Claude Code can leave the same subagent transcript reachable from more
    than one session's ``subagents/`` directory via a symlink -- ingesting
    every discovered path would double-count that subagent's API calls.
    Group subagent files by content (``jsonl_sha256``, hashed once here, and
    reused by the caller for ``extract_metadata`` -- finding 3) and keep only
    one entry per group, in this preference order:

    1. The member that *is* the real file (no symlink hop, per
       ``Path.resolve()``) -- stable and content-addressed, no drift risk.
    2. Failing that (every discovered path for this hash is itself a
       symlink), whichever member the DB already recorded as owner for this
       ``jsonl_sha256`` on a prior run -- pins ownership across runs instead
       of re-deriving it from whatever session/label set happens to be
       present *this* run (finding 1: that used to drift and leave stale
       rows behind).
    3. Only if neither applies (first time this hash is seen and no member is
       the real file) an arbitrary but deterministic tie-break by label. This
       choice becomes the pinned owner via (2) on the next run once loaded.

    Whenever the chosen owner differs from a previously recorded owner for
    this hash, the previous owner's row (and cascading children) is deleted
    so ownership can't accumulate double-counted duplicates over time.

    Main session transcripts are never deduplicated this way -- only
    subagent files are known to be symlinked in practice.
    """
    kept: list[TranscriptFile] = []
    dropped: list[tuple[TranscriptFile, TranscriptFile, bool]] = []
    others: list[TranscriptFile] = []
    hashes: dict[Path, str] = {}
    by_hash: dict[str, list[tuple[TranscriptFile, Path]]] = defaultdict(list)

    for t in transcripts:
        if not t.is_subagent or not t.path.is_file():
            others.append(t)
            continue
        try:
            digest = hash_file(t.path)
        except OSError:
            others.append(t)
            continue
        by_hash[digest].append((t, t.path.resolve()))

    for digest, group in by_hash.items():
        if len(group) == 1:
            owner = group[0][0]
            kept.append(owner)
            hashes[owner.path] = digest
            continue

        owners = [t for t, real in group if t.path == real]
        arbitrary = False
        if owners:
            owner = owners[0]
        else:
            prior = _prior_owner(conn, digest)
            prior_match = (
                next(
                    (t for t, _real in group if (str(t.session_id), t.agent_id) == prior),
                    None,
                )
                if prior is not None
                else None
            )
            if prior_match is not None:
                owner = prior_match
            else:
                owner = min(group, key=lambda pair: pair[0].label)[0]
                arbitrary = True

        _reconcile_stale_owner(conn, digest, owner)
        kept.append(owner)
        hashes[owner.path] = digest
        for t, _real in group:
            if t is not owner:
                dropped.append((t, owner, arbitrary))

    return others + kept, dropped, hashes


def _load_checkpoint(
    conn: Connection[Any], transcript: TranscriptFile
) -> TranscriptCheckpoint | None:
    """Read back the prior aggregate state for *transcript*, if any.

    Returns None when there is nothing to resume from (never ingested, or
    ingested before ``jsonl_prefix_bytes`` existed).
    """
    row = conn.execute(
        """
        SELECT transcript_id, jsonl_prefix_bytes, jsonl_sha256, record_count,
               malformed_lines, first_event_at, last_event_at, cwd, git_branches,
               cli_versions, models, is_sidechain, user_message_count,
               assistant_msg_count, api_call_count
          FROM claude.transcripts
         WHERE session_id = %s AND agent_id = %s
        """,
        (str(transcript.session_id), transcript.agent_id),
    ).fetchone()
    if row is None or row["jsonl_prefix_bytes"] is None or row["jsonl_sha256"] is None:
        return None

    type_rows = conn.execute(
        """
        SELECT record_type, record_subtype, record_count
          FROM claude.transcript_record_types
         WHERE transcript_id = %s
        """,
        (row["transcript_id"],),
    ).fetchall()
    record_types: Counter[tuple[str, str]] = Counter(
        {(r["record_type"], r["record_subtype"]): r["record_count"] for r in type_rows}
    )

    return TranscriptCheckpoint(
        prefix_bytes=int(row["jsonl_prefix_bytes"]),
        prefix_sha256=row["jsonl_sha256"],
        record_count=row["record_count"] or 0,
        malformed_lines=row["malformed_lines"] or 0,
        first_event_at=row["first_event_at"],
        last_event_at=row["last_event_at"],
        cwd=row["cwd"],
        git_branches=list(row["git_branches"] or []),
        cli_versions=list(row["cli_versions"] or []),
        models=list(row["models"] or []),
        is_sidechain=bool(row["is_sidechain"]),
        user_message_count=row["user_message_count"] or 0,
        assistant_msg_count=row["assistant_msg_count"] or 0,
        api_call_count=row["api_call_count"] or 0,
        record_types=record_types,
    )


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
    new_session_ids: set[str] = set()
    updated_session_ids: set[str] = set()
    started = time.monotonic()

    kept, duplicates, subagent_hashes = _dedupe_subagents(conn, list(transcripts))
    for duplicate, owner, arbitrary in duplicates:
        summary.discovered += 1
        summary.skipped += 1
        if on_progress:
            # "tie-broken" ownership was arbitrarily picked (no real file, no
            # prior DB owner for this hash) and could still move on a future
            # run if a new member appears; "resolved" ownership is pinned by
            # the real (non-symlink) path or a prior DB record and is stable.
            tag = "tie-broken" if arbitrary else "resolved"
            on_progress(
                f"skip  {duplicate.label} (duplicate of {owner.label} via "
                f"jsonl_sha256, {tag} owner; symlinked subagent transcript)"
            )

    for transcript in kept:
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

            checkpoint = None if force else _load_checkpoint(conn, transcript)
            meta = extract_metadata(
                transcript.path,
                checkpoint=checkpoint,
                precomputed_sha256=subagent_hashes.get(transcript.path),
            )

            if not force and _already_current(conn, transcript, meta.jsonl_sha256):
                summary.skipped += 1
                if on_progress:
                    on_progress(f"skip  {label} (unchanged)")
                continue

            project_id = upsert_project(conn, transcript.project_dir, meta.cwd)
            inserted = upsert_session(conn, transcript.session_id, project_id, meta)
            session_key = str(transcript.session_id)
            transcript_id = upsert_transcript(conn, transcript, meta)

            # Subagent transcripts are merged into the main transcript's report by
            # cache_report_per_call_timing.sh's own subagent-discovery logic when the
            # script runs against the *main* .jsonl. Running the report script directly
            # on a subagent file would mislabel its rows as agent=main/depth=0 and
            # double-insert its API calls (see handoff finding 1).
            if not transcript.is_subagent and meta.api_call_count > 0:
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
                summary.rows_loaded += loaded
                progress_msg = f"load  {label} -> {loaded} calls"
            else:
                progress_msg = f"meta  {label} (no API calls)"

            conn.commit()
            if inserted:
                new_session_ids.add(session_key)
            else:
                updated_session_ids.discard(session_key)
                if session_key not in new_session_ids:
                    updated_session_ids.add(session_key)

            summary.processed += 1
            if on_progress:
                on_progress(progress_msg)
        except Exception as exc:  # noqa: BLE001 - one bad transcript must not stop the run
            conn.rollback()
            summary.failed += 1
            summary.failures.append((label, f"{type(exc).__name__}: {exc}"))
            if on_progress:
                on_progress(f"FAIL  {label}: {exc}")

    relink_subagent_calls(conn)
    refresh_rollups(conn)
    elapsed = time.monotonic() - started
    record_interactions_upload(
        conn,
        run_id=run_id,
        time_spent=int(elapsed),
        new_sessions=len(new_session_ids),
        updated_sessions=len(updated_session_ids),
    )
    conn.commit()
    return summary
