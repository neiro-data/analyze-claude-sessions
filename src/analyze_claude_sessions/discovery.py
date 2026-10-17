"""Locate Claude Code transcripts under ~/.claude/projects.

Two layouts exist side by side::

    <project>/<session_id>.jsonl                          main session transcript
    <project>/<session_id>/subagents/agent-<id>.jsonl     one per spawned subagent

Subagent files repeat the parent's sessionId but contain a disjoint set of
requestIds, so both layouts must be ingested to account for all API calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TranscriptFile:
    """One .jsonl file, tagged with the session it belongs to."""

    path: Path
    session_id: UUID
    project_dir: str
    agent_id: str = ""

    @property
    def is_subagent(self) -> bool:
        return bool(self.agent_id)

    @property
    def kind(self) -> str:
        return "subagent" if self.agent_id else "session"

    @property
    def label(self) -> str:
        return f"{self.project_dir}/{self.session_id}" + (
            f"/{self.agent_id}" if self.agent_id else ""
        )

    @property
    def size_bytes(self) -> int:
        return self.path.stat().st_size


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def discover_transcripts(
    projects_dir: Path, project_filter: str | None = None
) -> list[TranscriptFile]:
    """Return every transcript under *projects_dir*, main files before subagents.

    Files whose session component is not a UUID are skipped: the loader keys on
    session_id, and Claude Code always names sessions after their UUID.
    """
    if not projects_dir.is_dir():
        raise FileNotFoundError(f"Claude projects directory not found: {projects_dir}")

    found: list[TranscriptFile] = []

    for path in sorted(projects_dir.glob("*/*.jsonl")):
        session_id = _as_uuid(path.stem)
        if session_id is None:
            continue
        project_dir = path.parent.name
        if project_filter and project_filter not in project_dir:
            continue
        found.append(TranscriptFile(path=path, session_id=session_id, project_dir=project_dir))

    for path in sorted(projects_dir.glob("*/*/subagents/*.jsonl")):
        session_id = _as_uuid(path.parents[1].name)
        if session_id is None:
            continue
        project_dir = path.parents[2].name
        if project_filter and project_filter not in project_dir:
            continue
        found.append(
            TranscriptFile(
                path=path,
                session_id=session_id,
                project_dir=project_dir,
                agent_id=path.stem,
            )
        )

    return found
