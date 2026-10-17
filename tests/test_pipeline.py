from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from analyze_claude_sessions import pipeline as pipeline_mod
from analyze_claude_sessions.config import Settings
from analyze_claude_sessions.discovery import TranscriptFile
from analyze_claude_sessions.pipeline import ingest_transcripts

from .conftest import SESSION_ID


class _FakeCursorResult:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


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


class _CheckpointConn(_FakeConn):
    """A `_FakeConn` that returns a realistic `claude.transcripts` checkpoint row.

    Exercises `_load_checkpoint`'s column access (finding 5) -- the default
    `_FakeConn` always returns `None` from `fetchone`, so no pipeline test ever
    ran the tail/incremental-read path end-to-end.
    """

    def __init__(
        self, transcript_row: dict[str, Any], record_type_rows: list[dict[str, Any]]
    ) -> None:
        super().__init__()
        self._transcript_row = transcript_row
        self._record_type_rows = record_type_rows

    def execute(self, query: str, params: Any = None, **kwargs: Any) -> _FakeCursorResult:
        if "FROM claude.transcripts" in query and "transcript_record_types" not in query:
            return _FakeCursorResult([self._transcript_row])
        if "FROM claude.transcript_record_types" in query:
            return _FakeCursorResult(self._record_type_rows)
        return _FakeCursorResult()


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
        nlp_embeddings_cache_dir=tmp_path / "nlp_cache",
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

    def _relink(*a: Any, **k: Any) -> int:
        calls.append("relink")
        return 0

    monkeypatch.setattr(pipeline_mod, "relink_subagent_calls", _relink)
    monkeypatch.setattr(
        pipeline_mod, "refresh_rollups", lambda *a, **k: calls.append("refresh_rollups")
    )
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
    summary = ingest_transcripts(conn, settings, [main, sub])  # type: ignore[arg-type]

    assert summary.failed == 0
    assert calls.count("upsert_transcript") == 2
    # Report generation/CSV load must run exactly once, for the main transcript only.
    assert calls.count("generate_report") == 1
    assert calls.count("read_report_csv") == 1
    assert calls.count("load_api_calls") == 1
    # Relink runs once, after every load and before the rollup refresh.
    assert calls.count("relink") == 1
    assert calls.index("relink") > max(i for i, c in enumerate(calls) if c == "load_api_calls")
    assert calls.index("relink") < calls.index("refresh_rollups")


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
    summary = ingest_transcripts(conn, settings, [main])  # type: ignore[arg-type]

    assert summary.failed == 1
    assert conn.rolled_back == 1
    # The session insert was rolled back with the transaction; it must not be
    # counted as new/updated in the audit row.
    assert captured["new_sessions"] == 0
    assert captured["updated_sessions"] == 0


def test_symlinked_subagent_transcript_attributed_to_one_session(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    transcript: Path,
    subagent_transcript: Path,
) -> None:
    """A subagent file reachable via two paths (symlink) must load exactly once,
    attributed to the session under whose directory the real file lives."""
    calls = _patch_common(monkeypatch)
    monkeypatch.setattr(pipeline_mod, "upsert_session", lambda *a, **k: True)
    monkeypatch.setattr(
        pipeline_mod, "generate_report", lambda *a, **k: (_ for _ in ()).throw(AssertionError())
    )

    other_session_id = UUID("11111111-2222-4333-8444-555566667777")
    other_subagents = transcript.parent.parent / str(other_session_id) / "subagents"
    other_subagents.mkdir(parents=True)
    symlink_path = other_subagents / subagent_transcript.name
    symlink_path.symlink_to(subagent_transcript)

    real_owner = TranscriptFile(
        path=subagent_transcript,
        session_id=SESSION_ID,
        project_dir="proj",
        agent_id=subagent_transcript.stem,
    )
    symlinked = TranscriptFile(
        path=symlink_path,
        session_id=other_session_id,
        project_dir="proj",
        agent_id=symlink_path.stem,
    )

    skips: list[str] = []
    conn = _FakeConn()
    targets = [real_owner, symlinked]
    summary = ingest_transcripts(conn, settings, targets, on_progress=skips.append)  # type: ignore[arg-type]

    assert summary.failed == 0
    # Only the owning session's copy is upserted; the duplicate is skipped.
    assert calls.count("upsert_transcript") == 1
    assert summary.skipped == 1
    assert any("duplicate" in msg for msg in skips)
    # The real (non-symlink) path resolves ownership deterministically -- tag
    # the skip as "resolved", not "tie-broken" (finding 2).
    assert any("resolved" in msg for msg in skips)


class _PriorOwnerConn(_FakeConn):
    """A `_FakeConn` that reports a prior DB owner for a given jsonl_sha256.

    Used to exercise finding 1's pinning: when no group member is the real
    (non-symlink) file, ownership should follow the DB's previously recorded
    owner rather than an arbitrary label sort, and any other row claiming the
    same hash should be reconciled away.
    """

    def __init__(self, prior_session_id: str, prior_agent_id: str) -> None:
        super().__init__()
        self._prior = {"session_id": prior_session_id, "agent_id": prior_agent_id}
        self.deletes: list[tuple[Any, ...]] = []

    def execute(self, query: str, params: Any = None, **kwargs: Any) -> _FakeCursorResult:
        if "DELETE FROM claude.transcripts" in query:
            self.deletes.append(params)
            return _FakeCursorResult()
        if "SELECT session_id, agent_id" in query:
            return _FakeCursorResult([self._prior])
        return _FakeCursorResult()


def test_dedupe_pins_ownership_to_the_prior_db_owner_when_no_real_path_exists(
    tmp_path: Path,
) -> None:
    """Finding 1: with two symlink-only paths for the same content, ownership must
    follow the DB's previously recorded owner (not an arbitrary label sort), and
    the stale owner's row must be reconciled away."""
    target = tmp_path / "content.jsonl"
    target.write_text('{"type": "user"}\n', encoding="utf-8")

    session_a = UUID("11111111-2222-4333-8444-555566667777")
    session_b = UUID("22222222-3333-4444-8555-666677778888")
    dir_a = tmp_path / str(session_a) / "subagents"
    dir_b = tmp_path / str(session_b) / "subagents"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    link_a = dir_a / "agent-a.jsonl"
    link_b = dir_b / "agent-b.jsonl"
    link_a.symlink_to(target)
    link_b.symlink_to(target)

    # Both discovered paths are symlinks (target itself isn't among the
    # discovered TranscriptFiles), so neither is "the real file" -- the prior
    # DB owner (session_a) must win over label order (which would pick
    # session_a's "agent-a" anyway by sort, so use B's label sorting first to
    # prove the DB pin -- not the sort -- decides it).
    t_a = TranscriptFile(path=link_a, session_id=session_a, project_dir="proj", agent_id="agent-a")
    # Label-sorts before t_a's -- if the fallback tie-break fired instead of
    # the DB pin, this member would be (wrongly) chosen as owner.
    t_b = TranscriptFile(
        path=link_b,
        session_id=UUID("00000000-0000-4000-8000-000000000000"),
        project_dir="proj",
        agent_id="agent-a",
    )

    conn = _PriorOwnerConn(str(session_a), "agent-a")
    kept, dropped, hashes = pipeline_mod._dedupe_subagents(conn, [t_a, t_b])  # type: ignore[arg-type]

    assert len(kept) == 1
    assert kept[0].session_id == session_a
    assert len(dropped) == 1
    _duplicate, owner, arbitrary = dropped[0]
    assert owner.session_id == session_a
    assert arbitrary is False  # pinned by the prior DB owner, not tie-broken
    assert conn.deletes  # the reconciliation delete ran


def test_load_checkpoint_reads_a_realistic_transcripts_row(transcript: Path) -> None:
    """Finding 5: exercise `_load_checkpoint`'s column access against a row shaped
    like a real `claude.transcripts` record, not the always-`None` `_FakeConn`."""
    transcript_row = {
        "transcript_id": 42,
        "jsonl_prefix_bytes": 123,
        "jsonl_sha256": "a" * 64,
        "record_count": 5,
        "malformed_lines": 0,
        "first_event_at": datetime(2026, 8, 8, 11, 49, 35, tzinfo=UTC),
        "last_event_at": datetime(2026, 8, 8, 11, 50, 53, tzinfo=UTC),
        "cwd": "/Users/dev/project",
        "git_branches": ["main"],
        "cli_versions": ["2.1.226"],
        "models": ["claude-opus-5"],
        "is_sidechain": False,
        "user_message_count": 1,
        "assistant_msg_count": 3,
        "api_call_count": 2,
    }
    record_type_rows = [
        {"record_type": "assistant", "record_subtype": "", "record_count": 3},
        {"record_type": "user", "record_subtype": "", "record_count": 1},
    ]
    conn = _CheckpointConn(transcript_row, record_type_rows)
    main = TranscriptFile(path=transcript, session_id=SESSION_ID, project_dir="proj")

    checkpoint = pipeline_mod._load_checkpoint(conn, main)  # type: ignore[arg-type]

    assert checkpoint is not None
    assert checkpoint.prefix_bytes == 123
    assert checkpoint.prefix_sha256 == "a" * 64
    assert checkpoint.api_call_count == 2
    assert dict(checkpoint.record_types) == {
        ("assistant", ""): 3,
        ("user", ""): 1,
    }


def test_tail_ingest_resumes_from_a_realistic_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    transcript: Path,
) -> None:
    """Finding 5 (end-to-end): a `_load_checkpoint` row whose prefix matches the
    file's current prefix must drive `extract_metadata` down the tail path."""
    calls = _patch_common(monkeypatch)
    monkeypatch.setattr(pipeline_mod, "upsert_session", lambda *a, **k: True)
    monkeypatch.setattr(pipeline_mod, "generate_report", lambda *a, **k: object())
    monkeypatch.setattr(pipeline_mod, "read_report_csv", lambda *a, **k: _FakeFrame())
    monkeypatch.setattr(pipeline_mod, "record_csv_file", lambda *a, **k: 1)
    monkeypatch.setattr(pipeline_mod, "load_api_calls", lambda *a, **k: 1)

    from analyze_claude_sessions.jsonl_meta import extract_metadata

    baseline = extract_metadata(transcript)
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(
            f'{{"type": "user", "sessionId": "{SESSION_ID}", "message": {{"role": "user", '
            '"content": "more"}}\n'
        )

    transcript_row = {
        "transcript_id": 7,
        "jsonl_prefix_bytes": baseline.jsonl_prefix_bytes,
        "jsonl_sha256": baseline.jsonl_sha256,
        "record_count": baseline.record_count,
        "malformed_lines": baseline.malformed_lines,
        "first_event_at": baseline.first_event_at,
        "last_event_at": baseline.last_event_at,
        "cwd": baseline.cwd,
        "git_branches": baseline.git_branches,
        "cli_versions": baseline.cli_versions,
        "models": baseline.models,
        "is_sidechain": baseline.is_sidechain,
        "user_message_count": baseline.user_message_count,
        "assistant_msg_count": baseline.assistant_msg_count,
        "api_call_count": baseline.api_call_count,
    }
    conn = _CheckpointConn(transcript_row, [])
    main = TranscriptFile(path=transcript, session_id=SESSION_ID, project_dir="proj")

    summary = ingest_transcripts(conn, settings, [main])  # type: ignore[arg-type]

    assert summary.failed == 0
    assert summary.processed == 1
    assert calls.count("upsert_transcript") == 1
