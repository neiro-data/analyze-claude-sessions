"""Extract session-level metadata by streaming a transcript once.

The CSV report only carries per-call token counts; everything that answers
"which project, which model, how long, how big" is read here and lands in
claude.sessions / claude.session_record_types.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson

_READ_CHUNK = 1 << 20


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
    is_sidechain: bool = False
    user_message_count: int = 0
    assistant_msg_count: int = 0
    api_call_count: int = 0
    record_types: Counter[tuple[str, str]] = field(default_factory=Counter)


def parse_timestamp(raw: object) -> datetime | None:
    """Parse an ISO-8601 transcript timestamp, tolerating the trailing ``Z``."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _note(bucket: list[str], value: object) -> None:
    if isinstance(value, str) and value and value not in bucket:
        bucket.append(value)


def extract_metadata(path: Path) -> SessionMetadata:
    """Stream *path* line by line and summarise it."""
    stat = path.stat()
    meta = SessionMetadata(
        jsonl_path=str(path),
        jsonl_bytes=stat.st_size,
        jsonl_mtime=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        jsonl_sha256=_digest(path),
    )
    request_ids: set[str] = set()
    cwd_votes: Counter[str] = Counter()

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
            _absorb(meta, record, request_ids, cwd_votes)

    meta.api_call_count = len(request_ids)
    if cwd_votes:
        meta.cwd = cwd_votes.most_common(1)[0][0]
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
    elif record_type == "assistant":
        meta.assistant_msg_count += 1
        message = record.get("message")
        if isinstance(message, dict):
            _note(meta.models, message.get("model"))
            if message.get("usage") and (request_id := record.get("requestId")):
                request_ids.add(str(request_id))
