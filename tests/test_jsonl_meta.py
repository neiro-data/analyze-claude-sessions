from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from analyze_claude_projects.jsonl_meta import extract_metadata, parse_timestamp

from .conftest import SESSION_ID


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


def test_parse_timestamp_handles_z_suffix_and_junk() -> None:
    assert parse_timestamp("2026-08-08T11:49:40.410Z") == datetime(
        2026, 8, 8, 11, 49, 40, 410000, tzinfo=UTC
    )
    assert parse_timestamp("not-a-date") is None
    assert parse_timestamp(None) is None
