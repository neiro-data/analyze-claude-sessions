from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from analyze_claude_projects import pipeline as pipeline_mod
from analyze_claude_projects.config import Settings
from analyze_claude_projects.discovery import TranscriptFile
from analyze_claude_projects.pipeline import ingest_transcripts

from .conftest import SESSION_ID


class _FakeCursorResult:
    def fetchone(self) -> None:
        return None


class _FakeConn:
    """Stands in for psycopg.Connection: only tracks commit/rollback calls."""

    def __init__(self) -> None:
        self.committed = 0
        self.rolled_back = 0

    def execute(self, *args: Any, **kwargs: Any) -> _FakeCursorResult:
        return _FakeCursorResult()

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        projects_dir=tmp_path,
        data_dir=tmp_path,
        csv_dir=tmp_path / "csv",
        parquet_dir=tmp_path / "parquet",
        parquet_dir_in_db="/parquet",
        report_script=tmp_path / "unused.sh",
        sql_dir=tmp_path / "sql",
        database_url="postgresql://unused/unused",
    )


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch every DB/report call pipeline.py makes; return a call-order log."""
    calls: list[str] = []

    monkeypatch.setattr(pipeline_mod, "upsert_project", lambda *a, **k: 1)

    def _upsert_transcript(*a: Any, **k: Any) -> int:
        calls.append("upsert_transcript")
        return 1

    monkeypatch.setattr(pipeline_mod, "upsert_transcript", _upsert_transcript)
    monkeypatch.setattr(pipeline_mod, "record_interactions_upload", lambda *a, **k: None)
    monkeypatch.setattr(pipeline_mod, "refresh_rollups", lambda *a, **k: None)
    return calls


def test_subagent_transcripts_skip_report_generation(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    transcript: Path,
    subagent_transcript: Path,
) -> None:
    """Fix 1: only the main transcript should trigger generate_report/read_report_csv/load."""
    calls = _patch_common(monkeypatch)

    def _generate_report(*a: Any, **k: Any) -> object:
        calls.append("generate_report")
        return object()

    def _read_report_csv(*a: Any, **k: Any) -> _FakeFrame:
        calls.append("read_report_csv")
        return _FakeFrame()

    def _load_api_calls(*a: Any, **k: Any) -> int:
        calls.append("load_api_calls")
        return 1

    monkeypatch.setattr(pipeline_mod, "upsert_session", lambda *a, **k: True)
    monkeypatch.setattr(pipeline_mod, "generate_report", _generate_report)
    monkeypatch.setattr(pipeline_mod, "read_report_csv", _read_report_csv)
    monkeypatch.setattr(pipeline_mod, "record_csv_file", lambda *a, **k: 1)
    monkeypatch.setattr(pipeline_mod, "load_api_calls", _load_api_calls)

    main = TranscriptFile(path=transcript, session_id=SESSION_ID, project_dir="proj")
    sub = TranscriptFile(
        path=subagent_transcript,
        session_id=SESSION_ID,
        project_dir="proj",
        agent_id="agent-a550490847ef81a58",
    )

    conn = _FakeConn()
    summary = ingest_transcripts(conn, settings, [main, sub])

    assert summary.failed == 0
    assert calls.count("upsert_transcript") == 2
    # Report generation/CSV load must run exactly once, for the main transcript only.
    assert calls.count("generate_report") == 1
    assert calls.count("read_report_csv") == 1
    assert calls.count("load_api_calls") == 1


class _FakeFrame:
    height = 1


def test_session_counters_not_inflated_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    transcript: Path,
) -> None:
    """Fix 2: a failure after upsert_session must not count the session as new/updated."""
    _patch_common(monkeypatch)

    monkeypatch.setattr(pipeline_mod, "upsert_session", lambda *a, **k: True)
    monkeypatch.setattr(
        pipeline_mod,
        "generate_report",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    captured: dict[str, Any] = {}

    def _fake_record_interactions_upload(
        conn: Any, *, run_id: Any, time_spent: Any, new_sessions: Any, updated_sessions: Any
    ) -> None:
        captured["new_sessions"] = new_sessions
        captured["updated_sessions"] = updated_sessions

    monkeypatch.setattr(
        pipeline_mod, "record_interactions_upload", _fake_record_interactions_upload
    )

    main = TranscriptFile(path=transcript, session_id=SESSION_ID, project_dir="proj")

    conn = _FakeConn()
    summary = ingest_transcripts(conn, settings, [main])

    assert summary.failed == 1
    assert conn.rolled_back == 1
    # The session insert was rolled back with the transaction; it must not be
    # counted as new/updated in the audit row.
    assert captured["new_sessions"] == 0
    assert captured["updated_sessions"] == 0
