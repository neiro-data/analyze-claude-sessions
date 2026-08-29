from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from unittest import mock

from analyze_claude_projects.jsonl_meta import (
    SessionMetadata,
    TranscriptCheckpoint,
    extract_metadata,
    parse_timestamp,
)

from .conftest import SESSION_ID


def _checkpoint_from(meta: SessionMetadata) -> TranscriptCheckpoint:
    """Build the checkpoint the loader would have persisted from a prior read."""
    return TranscriptCheckpoint(
        prefix_bytes=meta.jsonl_prefix_bytes,
        prefix_sha256=meta.jsonl_sha256,
        record_count=meta.record_count,
        malformed_lines=meta.malformed_lines,
        first_event_at=meta.first_event_at,
        last_event_at=meta.last_event_at,
        cwd=meta.cwd,
        git_branches=list(meta.git_branches),
        cli_versions=list(meta.cli_versions),
        models=list(meta.models),
        is_sidechain=meta.is_sidechain,
        user_message_count=meta.user_message_count,
        assistant_msg_count=meta.assistant_msg_count,
        api_call_count=meta.api_call_count,
        record_types=meta.record_types,
    )


def test_extracts_session_shape(transcript: Path) -> None:
    meta = extract_metadata(transcript)

    assert meta.record_count == 5
    assert meta.malformed_lines == 0
    assert meta.user_message_count == 1
    assert meta.assistant_msg_count == 3
    # Three assistant records, two distinct requestIds -> two API calls.
    assert meta.api_call_count == 2
    assert meta.cwd == "/Users/dev/project"
    assert meta.models == ["claude-opus-5"]
    assert meta.cli_versions == ["2.1.226"]
    assert meta.git_branches == ["main"]
    assert meta.ai_title == "Test session"
    assert meta.is_sidechain is False


def test_extracts_time_window_and_digest(transcript: Path) -> None:
    meta = extract_metadata(transcript)

    assert meta.first_event_at == datetime(2026, 8, 8, 11, 49, 35, tzinfo=UTC)
    assert meta.last_event_at == datetime(2026, 8, 8, 11, 50, 53, 438000, tzinfo=UTC)
    assert len(meta.jsonl_sha256) == 64
    assert meta.jsonl_bytes == transcript.stat().st_size


def test_counts_record_types(transcript: Path) -> None:
    meta = extract_metadata(transcript)

    assert meta.record_types[("assistant", "")] == 3
    assert meta.record_types[("user", "")] == 1
    assert meta.record_types[("ai-title", "")] == 1


def test_malformed_lines_are_counted_not_fatal(transcript: Path) -> None:
    """The shell report gives up on a corrupt transcript; metadata extraction does not."""
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write("{not json}\n")

    meta = extract_metadata(transcript)

    assert meta.malformed_lines == 1
    assert meta.record_count == 5


def _prepend_user_record(
    transcript: Path, content: str, sidechain_session_id: str | None = None
) -> None:
    """Insert a ``user`` record with *content* at the very start of the fixture transcript."""
    record: dict[str, object] = {
        "type": "user",
        "uuid": "aaaaaaaa-0000-4000-8000-000000000001",
        "timestamp": "2026-08-08T11:49:34.000Z",
        "sessionId": sidechain_session_id or str(SESSION_ID),
        "message": {"role": "user", "content": content},
    }
    lines = transcript.read_text(encoding="utf-8").splitlines()
    lines.insert(0, json.dumps(record))
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")


ORIGIN_ID = "a9785b16-3425-46c9-aea3-3ae920bed50c"


def test_origin_session_id_captured_at_start_of_first_prompt(transcript: Path) -> None:
    _prepend_user_record(transcript, f"Origin session_id: {ORIGIN_ID}. Read the plan.")

    meta = extract_metadata(transcript)

    assert meta.origin_session_id == ORIGIN_ID


def test_origin_session_id_ignored_mid_message(transcript: Path) -> None:
    _prepend_user_record(transcript, f"See discussion of Origin session_id: {ORIGIN_ID} below.")

    meta = extract_metadata(transcript)

    assert meta.origin_session_id is None


def test_origin_session_id_ignored_on_last_prompt_record(transcript: Path) -> None:
    lines = transcript.read_text(encoding="utf-8").splitlines()
    lines.insert(
        0,
        json.dumps(
            {
                "type": "last-prompt",
                "lastPrompt": f"Origin session_id: {ORIGIN_ID}. Read the …",
            }
        ),
    )
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

    meta = extract_metadata(transcript)

    assert meta.origin_session_id is None


def test_origin_session_id_self_reference_ignored(transcript: Path) -> None:
    _prepend_user_record(transcript, f"Origin session_id: {SESSION_ID}. Read the plan.")

    meta = extract_metadata(transcript)

    assert meta.origin_session_id is None


def test_origin_session_id_absent_by_default(transcript: Path) -> None:
    meta = extract_metadata(transcript)

    assert meta.origin_session_id is None


class _SeekTrackingFile:
    """Wraps a real binary file object, recording every ``seek()`` offset."""

    def __init__(self, handle: BinaryIO, offsets: list[int]) -> None:
        self._handle = handle
        self._offsets = offsets

    def __enter__(self) -> _SeekTrackingFile:
        return self

    def __exit__(self, *exc: object) -> None:
        self._handle.close()

    def __iter__(self) -> Iterator[bytes]:
        return iter(self._handle)

    def __getattr__(self, name: str) -> object:
        return getattr(self._handle, name)

    def seek(self, offset: int, *a: int) -> int:
        self._offsets.append(offset)
        return self._handle.seek(offset, *a)


def _seek_offsets_during(fn: Callable[[], object]) -> list[int]:
    """Record every byte offset `Path.open(...).seek()` is called with during *fn*."""
    offsets: list[int] = []
    real_open = Path.open

    def _patched_open(self: Path, mode: str = "r", **kwargs: object) -> _SeekTrackingFile:
        handle = real_open(self, mode, **kwargs)  # type: ignore[call-overload]
        return _SeekTrackingFile(handle, offsets)

    with mock.patch.object(Path, "open", _patched_open):
        fn()
    return offsets


def test_checkpoint_prefix_covers_whole_file(transcript: Path) -> None:
    meta = extract_metadata(transcript)

    assert meta.jsonl_prefix_bytes == transcript.stat().st_size
    assert meta.jsonl_prefix_lines == meta.record_count + meta.malformed_lines


def test_appended_lines_are_parsed_from_the_tail_only(transcript: Path) -> None:
    """A grown file whose prefix still matches should only stream the tail."""
    first = extract_metadata(transcript)
    checkpoint = _checkpoint_from(first)

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "11111111-2222-4333-8444-555566667777",
                    "timestamp": "2026-08-08T11:51:10.000Z",
                    "sessionId": str(SESSION_ID),
                    "requestId": "req_3",
                    "message": {
                        "role": "assistant",
                        "model": "claude-opus-5",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                }
            )
            + "\n"
        )

    # A patched `Path.read_text`/whole-file read would still succeed with the
    # tail logic (it uses `open(...).seek()` internally, not a full read), so
    # correctness is asserted via content instead of instrumenting the seek --
    # CPython's `_io.BufferedReader` is immutable and cannot be monkeypatched.
    tail = extract_metadata(transcript, checkpoint=checkpoint)

    seek_offsets = _seek_offsets_during(lambda: extract_metadata(transcript, checkpoint=checkpoint))
    assert seek_offsets == [checkpoint.prefix_bytes]

    assert tail.record_count == first.record_count + 1
    assert tail.api_call_count == first.api_call_count + 1
    assert tail.last_event_at == datetime(2026, 8, 8, 11, 51, 10, tzinfo=UTC)
    assert tail.first_event_at == first.first_event_at
    assert tail.jsonl_prefix_bytes == transcript.stat().st_size

    full_reread = extract_metadata(transcript)
    assert tail.record_count == full_reread.record_count
    assert tail.api_call_count == full_reread.api_call_count
    assert tail.jsonl_sha256 == full_reread.jsonl_sha256
    assert dict(tail.record_types) == dict(full_reread.record_types)


def test_truncated_file_falls_back_to_a_full_reread(transcript: Path) -> None:
    """A prefix mismatch (rewrite/truncation) must not resume from the old offset."""
    first = extract_metadata(transcript)
    checkpoint = _checkpoint_from(first)

    transcript.write_text('{"type": "user", "sessionId": "not-the-old-content"}\n')

    meta = extract_metadata(transcript, checkpoint=checkpoint)

    assert meta.record_count == 1
    assert meta.jsonl_prefix_bytes == transcript.stat().st_size


def test_parse_timestamp_handles_z_suffix_and_junk() -> None:
    assert parse_timestamp("2026-08-08T11:49:40.410Z") == datetime(
        2026, 8, 8, 11, 49, 40, 410000, tzinfo=UTC
    )
    assert parse_timestamp("not-a-date") is None
    assert parse_timestamp(None) is None
