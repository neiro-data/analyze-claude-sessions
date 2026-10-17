from __future__ import annotations

from pathlib import Path

import pytest

from analyze_claude_sessions.discovery import discover_transcripts

from .conftest import SESSION_ID


def test_discovers_uuid_named_transcripts(transcript: Path) -> None:
    found = discover_transcripts(transcript.parents[1])

    assert [item.session_id for item in found] == [SESSION_ID]
    assert found[0].project_dir == "-Users-dev-project"
    assert found[0].kind == "session"
    assert found[0].agent_id == ""


def test_discovers_subagent_transcripts_under_their_parent(subagent_transcript: Path) -> None:
    found = discover_transcripts(subagent_transcript.parents[3])

    kinds = [item.kind for item in found]
    # Main transcripts are listed before the subagents they spawned.
    assert kinds == ["session", "subagent"]

    subagent = found[1]
    assert subagent.session_id == SESSION_ID
    assert subagent.agent_id == "agent-a550490847ef81a58"
    assert subagent.project_dir == "-Users-dev-project"
    assert subagent.is_subagent
    assert subagent.label.endswith("/agent-a550490847ef81a58")


def test_ignores_non_uuid_filenames(transcript: Path) -> None:
    (transcript.parent / "notes.jsonl").write_text("{}\n", encoding="utf-8")

    assert len(discover_transcripts(transcript.parents[1])) == 1


def test_project_filter_narrows_results(subagent_transcript: Path) -> None:
    root = subagent_transcript.parents[3]

    assert len(discover_transcripts(root, "dev-project")) == 2
    assert discover_transcripts(root, "nope") == []


def test_missing_root_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        discover_transcripts(tmp_path / "absent")
