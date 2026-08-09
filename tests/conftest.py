from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

SESSION_ID = UUID("bb5714bc-8305-4138-818a-3a96bcfe6549")

CSV_HEADER = (
    "call,timestamp,uuid,input_tokens,cache_write_tokens,cache_read_tokens,output_tokens,"
    "delta_seconds,delta_seconds_global,agent,agent_type,depth,parent_agent,model,"
    "tool_use_id,description"
)

CSV_BODY = """\
1,"2026-08-08T11:49:40.410Z","306c344f-4993-478a-b3a6-dea410e2a026",10,8039,10090,605,0,0,main,main,0,,claude-opus-5,,
2,"2026-08-08T11:50:53.438Z","cde35a4f-6b3a-4b8c-9ba8-9103c642b593",10,3982,18741,614,73,73,main,main,0,,claude-opus-5,,
"""

CSV_REPORT = CSV_HEADER + "\n" + CSV_BODY


def _record(**overrides: object) -> str:
    base: dict[str, object] = {
        "type": "assistant",
        "uuid": "306c344f-4993-478a-b3a6-dea410e2a026",
        "parentUuid": "265394c6-8523-4349-b1be-234c38a25d9f",
        "timestamp": "2026-08-08T11:49:40.410Z",
        "sessionId": str(SESSION_ID),
        "cwd": "/Users/dev/project",
        "gitBranch": "main",
        "version": "2.1.226",
        "isSidechain": False,
        "requestId": "req_1",
        "message": {
            "role": "assistant",
            "model": "claude-opus-5",
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 8039,
                "cache_read_input_tokens": 10090,
                "output_tokens": 605,
            },
        },
    }
    base.update(overrides)
    return json.dumps(base)


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    """A minimal but realistic transcript: 2 API calls across 3 assistant records."""
    project_dir = tmp_path / "-Users-dev-project"
    project_dir.mkdir()
    path = project_dir / f"{SESSION_ID}.jsonl"
    lines = [
        json.dumps(
            {
                "type": "user",
                "uuid": "265394c6-8523-4349-b1be-234c38a25d9f",
                "timestamp": "2026-08-08T11:49:35.000Z",
                "cwd": "/Users/dev/project",
                "gitBranch": "main",
                "version": "2.1.226",
                "message": {"role": "user", "content": "hi"},
            }
        ),
        _record(),
        # Same requestId -> same API call, must not be double counted.
        _record(uuid="0e1f5a5e-2a1a-4c5e-9d21-1b2c3d4e5f60"),
        _record(
            uuid="cde35a4f-6b3a-4b8c-9ba8-9103c642b593",
            requestId="req_2",
            timestamp="2026-08-08T11:50:53.438Z",
        ),
        json.dumps({"type": "ai-title", "aiTitle": "Test session", "sessionId": str(SESSION_ID)}),
        "",  # blank lines are tolerated by both the shell report and the metadata reader
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def subagent_transcript(transcript: Path) -> Path:
    """A subagent file beside its parent: <project>/<session_id>/subagents/agent-x.jsonl."""
    subagents = transcript.parent / str(SESSION_ID) / "subagents"
    subagents.mkdir(parents=True)
    path = subagents / "agent-a550490847ef81a58.jsonl"
    path.write_text(
        _record(
            uuid="fec3d13b-1579-48a0-bc0d-6642a1bc78e9",
            requestId="req_sub_1",
            isSidechain=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def csv_report(tmp_path: Path) -> Path:
    path = tmp_path / f"{SESSION_ID}_20260808T11:49:35.csv"
    path.write_text(CSV_REPORT, encoding="utf-8")
    return path
