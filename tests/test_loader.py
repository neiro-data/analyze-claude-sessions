from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from analyze_claude_sessions.loader import LoadError, read_report_csv
from analyze_claude_sessions.report import CsvReport

from .conftest import SESSION_ID


def _report(path: Path) -> CsvReport:
    return CsvReport(path=path, stem=str(SESSION_ID), generated_at=None)


def test_reads_reference_csv_into_staging_layout(csv_report: Path) -> None:
    frame = read_report_csv(_report(csv_report))

    assert frame.columns == [
        "call_index",
        "called_at",
        "message_uuid",
        "input_tokens",
        "cache_write_tokens",
        "cache_read_tokens",
        "output_tokens",
        "delta_seconds",
        "delta_seconds_global",
        "depth",
        "agent",
        "agent_type",
        "parent_agent",
        "model",
        "tool_use_id",
        "description",
        "usage_records",
        "output_tokens_raw",
    ]
    # column order mirrors _INT_COLUMNS then _TEXT_COLUMNS in loader.py
    assert frame.height == 2
    first = frame.row(0, named=True)
    assert first["call_index"] == 1
    assert first["called_at"] == datetime(2026, 8, 8, 11, 49, 40, 410000, tzinfo=UTC)
    assert first["message_uuid"] == "306c344f-4993-478a-b3a6-dea410e2a026"
    assert first["cache_read_tokens"] == 10090
    assert first["agent"] == "main"
    assert first["agent_type"] == "main"
    assert first["depth"] == 0
    assert first["model"] == "claude-opus-5"
    assert first["parent_agent"] is None
    assert first["tool_use_id"] is None
    assert first["description"] is None
    assert frame.row(1, named=True)["delta_seconds"] == 73
    assert frame.row(1, named=True)["delta_seconds_global"] == 73


def test_blank_text_columns_become_null(tmp_path: Path) -> None:
    path = tmp_path / f"{SESSION_ID}_20260808T11:49:35.csv"
    path.write_text(
        "call,timestamp,uuid,input_tokens,cache_write_tokens,cache_read_tokens,"
        "output_tokens,delta_seconds,delta_seconds_global,agent,agent_type,depth,"
        "parent_agent,model,tool_use_id,description\n"
        '1,"2026-08-08T11:49:40.410Z","306c344f-4993-478a-b3a6-dea410e2a026",'
        "1,2,3,4,0,0,,,0,,,,\n",
        encoding="utf-8",
    )

    row = read_report_csv(_report(path)).row(0, named=True)
    assert row["agent"] is None
    assert row["agent_type"] is None
    assert row["parent_agent"] is None
    assert row["model"] is None
    assert row["tool_use_id"] is None
    assert row["description"] is None


def test_missing_columns_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / f"{SESSION_ID}_20260808T11:49:35.csv"
    path.write_text("call,timestamp\n1,x\n", encoding="utf-8")

    with pytest.raises(LoadError, match="missing columns"):
        read_report_csv(_report(path))
