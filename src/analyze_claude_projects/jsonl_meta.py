"""Extract session-level metadata by streaming a transcript once.

The CSV report only carries per-call token counts; everything that answers
"which project, which model, how long, how big" is read here and lands in
claude.sessions / claude.session_record_types.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import orjson

_READ_CHUNK = 1 << 20

# Matches the marker the clear-cache-sonnet-low handoff prepends to a child
# session's first user prompt: `echo "Origin session_id: ${SESSION_ID}"`.
# Anchored at the start so a session merely *discussing* the marker mid-
# conversation doesn't false-positive.
_ORIGIN_SESSION_RE = re.compile(r"^\s*Origin session_id:\s*([0-9a-fA-F-]{36})", re.IGNORECASE)


@dataclass(slots=True)
class SessionMetadata:
    """Everything we can learn about a transcript without interpreting message bodies."""

    jsonl_path: str
    jsonl_bytes: int
    jsonl_mtime: datetime
    jsonl_sha256: str
    record_count: int = 0
    malformed_lines: int = 0
    first_event_at: datetime | None = None
    last_event_at: datetime | None = None
    cwd: str | None = None
    git_branches: list[str] = field(default_factory=list)
    cli_versions: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    ai_title: str | None = None
    origin_session_id: str | None = None
    is_sidechain: bool = False
    user_message_count: int = 0
    assistant_msg_count: int = 0
    api_call_count: int = 0
    record_types: Counter[tuple[str, str]] = field(default_factory=Counter)
    # Checkpoint for the next incremental read: how much of the file this
    # metadata accounts for. Always set to the full file on return.
    jsonl_prefix_bytes: int = 0
    jsonl_prefix_lines: int = 0


@dataclass(slots=True)
class TranscriptCheckpoint:
    """Prior aggregate state read back from ``claude.transcripts``.

    Lets :func:`extract_metadata` resume from the last recorded byte offset
    instead of re-parsing the whole file when it has only grown by having
    lines appended to it.
    """

    prefix_bytes: int
    prefix_sha256: str
    record_count: int
    malformed_lines: int
    first_event_at: datetime | None
    last_event_at: datetime | None
    cwd: str | None
    git_branches: list[str]
    cli_versions: list[str]
    models: list[str]
    is_sidechain: bool
    user_message_count: int
    assistant_msg_count: int
    api_call_count: int
    record_types: Counter[tuple[str, str]]


def parse_timestamp(raw: object) -> datetime | None:
    """Parse an ISO-8601 transcript timestamp, tolerating the trailing ``Z``."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def hash_file(path: Path) -> str:
    """Sha256 of *path*'s full contents. Public: reused for dedup by content."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


# Retained as a private alias so the rest of this module reads naturally.
_digest = hash_file


def _digest_with_prefix(path: Path, prefix_bytes: int) -> tuple[str, str]:
    """Hash the whole file and its first *prefix_bytes* bytes in one streaming pass.

    Replaces what used to be two near-full-file reads (a full ``_digest`` plus a
    separate ``_digest_prefix`` up to the checkpoint boundary): both digests are
    accumulated together as the file streams past once, which matters for the
    tail-ingest path where re-reading most of the file twice would undercut the
    I/O savings incremental ingest is meant to provide.
    """
    full_digest = hashlib.sha256()
    prefix_digest = hashlib.sha256()
    remaining_prefix = prefix_bytes
    with path.open("rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            if remaining_prefix > 0:
                take = min(remaining_prefix, len(chunk))
                prefix_digest.update(chunk[:take])
                remaining_prefix -= take
            full_digest.update(chunk)
    return full_digest.hexdigest(), prefix_digest.hexdigest()


def _extract_origin_session_id(message: object) -> str | None:
    """Pull the ``Origin session_id: <uuid>`` marker off a ``user`` message body.

    ``message.content`` is either a plain string or a list of content blocks;
    only the first ``text`` block is inspected, matching how the handoff
    wrapper prepends the marker as plain text ahead of the actual prompt.
    """
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    text: str | None = None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                block_text = block.get("text")
                if isinstance(block_text, str):
                    text = block_text
                break
    if not text:
        return None
    match = _ORIGIN_SESSION_RE.match(text)
    if not match:
        return None
    candidate = match.group(1)
    try:
        return str(UUID(candidate))
    except ValueError:
        return None


def _note(bucket: list[str], value: object) -> None:
    if isinstance(value, str) and value and value not in bucket:
        bucket.append(value)


def extract_metadata(
    path: Path,
    checkpoint: TranscriptCheckpoint | None = None,
    precomputed_sha256: str | None = None,
) -> SessionMetadata:
    """Stream *path* and summarise it.

    When *checkpoint* is given and the file has only grown (its first
    ``checkpoint.prefix_bytes`` bytes still hash to ``checkpoint.prefix_sha256``),
    only the appended tail is parsed and merged onto the checkpoint's
    aggregates. Otherwise (no checkpoint, file shrank, or the prefix no longer
    matches -- a rewrite or truncation) the whole file is re-read from scratch.

    *precomputed_sha256* lets a caller that already hashed the full file this
    run (e.g. ``_dedupe_subagents`` grouping by content) skip re-hashing it
    here; it is only used on the no-checkpoint/full-reread path -- the
    checkpoint path always (re)computes both digests in a single pass anyway,
    so there is nothing to save there.
    """
    stat = path.stat()

    if checkpoint is not None and stat.st_size >= checkpoint.prefix_bytes:
        full_sha256, prefix_sha256 = _digest_with_prefix(path, checkpoint.prefix_bytes)
        if prefix_sha256 == checkpoint.prefix_sha256:
            return _extract_from_offset(path, stat, full_sha256, checkpoint)
        return _extract_full(path, stat, full_sha256)

    full_sha256 = precomputed_sha256 or _digest(path)
    return _extract_full(path, stat, full_sha256)


def _extract_full(path: Path, stat: Any, full_sha256: str) -> SessionMetadata:
    meta = SessionMetadata(
        jsonl_path=str(path),
        jsonl_bytes=stat.st_size,
        jsonl_mtime=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        jsonl_sha256=full_sha256,
    )
    request_ids: set[str] = set()
    cwd_votes: Counter[str] = Counter()
    own_session_ids: set[str] = set()

    with path.open("rb") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record: Any = orjson.loads(line)
            except orjson.JSONDecodeError:
                meta.malformed_lines += 1
                continue
            if not isinstance(record, dict):
                meta.malformed_lines += 1
                continue

            meta.record_count += 1
            if session_id := record.get("sessionId"):
                if isinstance(session_id, str):
                    own_session_ids.add(session_id.lower())
            _absorb(meta, record, request_ids, cwd_votes)

    if meta.origin_session_id and meta.origin_session_id.lower() in own_session_ids:
        meta.origin_session_id = None

    meta.api_call_count = len(request_ids)
    if cwd_votes:
        meta.cwd = cwd_votes.most_common(1)[0][0]
    meta.jsonl_prefix_bytes = stat.st_size
    meta.jsonl_prefix_lines = meta.record_count + meta.malformed_lines
    return meta


def _extract_from_offset(
    path: Path, stat: Any, full_sha256: str, checkpoint: TranscriptCheckpoint
) -> SessionMetadata:
    """Parse only the bytes appended after ``checkpoint.prefix_bytes``.

    ``origin_session_id``/``ai_title`` are not seeded from the checkpoint --
    those live on ``claude.sessions``, are set-once (``COALESCE``d), and
    almost always appear in a transcript's first lines, so re-detecting them
    from the tail is unnecessary; a rare later occurrence is still picked up
    since the tail is run through the same ``_absorb`` logic.
    """
    meta = SessionMetadata(
        jsonl_path=str(path),
        jsonl_bytes=stat.st_size,
        jsonl_mtime=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        jsonl_sha256=full_sha256,
        record_count=checkpoint.record_count,
        malformed_lines=checkpoint.malformed_lines,
        first_event_at=checkpoint.first_event_at,
        last_event_at=checkpoint.last_event_at,
        cwd=checkpoint.cwd,
        git_branches=list(checkpoint.git_branches),
        cli_versions=list(checkpoint.cli_versions),
        models=list(checkpoint.models),
        is_sidechain=checkpoint.is_sidechain,
        user_message_count=checkpoint.user_message_count,
        assistant_msg_count=checkpoint.assistant_msg_count,
        record_types=Counter(checkpoint.record_types),
    )
    request_ids: set[str] = set()
    cwd_votes: Counter[str] = Counter()
    own_session_ids: set[str] = set()

    with path.open("rb") as handle:
        handle.seek(checkpoint.prefix_bytes)
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record: Any = orjson.loads(line)
            except orjson.JSONDecodeError:
                meta.malformed_lines += 1
                continue
            if not isinstance(record, dict):
                meta.malformed_lines += 1
                continue

            meta.record_count += 1
            if session_id := record.get("sessionId"):
                if isinstance(session_id, str):
                    own_session_ids.add(session_id.lower())
            _absorb(meta, record, request_ids, cwd_votes)

    if meta.origin_session_id and meta.origin_session_id.lower() in own_session_ids:
        meta.origin_session_id = None

    meta.api_call_count = checkpoint.api_call_count + len(request_ids)
    if cwd_votes:
        # Re-vote using the checkpoint's cwd as one candidate (weighted 1, since
        # the checkpoint doesn't carry the full historical vote tally) rather
        # than only falling back to it when the tail has no cwd at all -- a
        # true majority over the whole file (as `_extract_full` computes) could
        # differ from the checkpoint's cwd if the session `cd`'d mid-transcript,
        # and the tail should converge toward that rather than pin the first
        # value forever once one exists.
        if checkpoint.cwd:
            cwd_votes[checkpoint.cwd] += 1
        meta.cwd = cwd_votes.most_common(1)[0][0]
    meta.jsonl_prefix_bytes = stat.st_size
    meta.jsonl_prefix_lines = meta.record_count + meta.malformed_lines
    return meta


def _absorb(
    meta: SessionMetadata,
    record: dict[str, Any],
    request_ids: set[str],
    cwd_votes: Counter[str],
) -> None:
    record_type = str(record.get("type") or "unknown")
    subtype = record.get("subtype")
    meta.record_types[(record_type, str(subtype) if subtype else "")] += 1

    if ts := parse_timestamp(record.get("timestamp")):
        if meta.first_event_at is None or ts < meta.first_event_at:
            meta.first_event_at = ts
        if meta.last_event_at is None or ts > meta.last_event_at:
            meta.last_event_at = ts

    if cwd := record.get("cwd"):
        if isinstance(cwd, str):
            cwd_votes[cwd] += 1
    _note(meta.git_branches, record.get("gitBranch"))
    _note(meta.cli_versions, record.get("version"))

    if record.get("isSidechain") is True:
        meta.is_sidechain = True

    if record_type == "ai-title":
        title = record.get("aiTitle")
        if isinstance(title, str) and title:
            meta.ai_title = title
    elif record_type == "user":
        meta.user_message_count += 1
        if meta.origin_session_id is None:
            meta.origin_session_id = _extract_origin_session_id(record.get("message"))
    elif record_type == "assistant":
        meta.assistant_msg_count += 1
        message = record.get("message")
        if isinstance(message, dict):
            _note(meta.models, message.get("model"))
            if message.get("usage") and (request_id := record.get("requestId")):
                request_ids.add(str(request_id))
