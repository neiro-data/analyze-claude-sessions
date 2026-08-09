from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from analyze_claude_projects.loader import LoadError, read_report_csv
from analyze_claude_projects.report import CsvReport

from .conftest import SESSION_ID


def _report(path: Path) -> CsvReport:
    return CsvReport(path=path, stem=str(SESSION_ID), generated_at=None)


def test_reads_reference_csv_into_staging_layout(csv_with_parent: Path) -> None:
    frame = read_report_csv(_report(csv_with_parent))

    assert frame.columns == [
        "call_index",
        "called_at",
        "message_uuid",
        "parent_uuid",
        "input_tokens",
        "cache_write_tokens",
        "cache_read_tokens",
        "output_tokens",
        "delta_seconds",
    ]
    assert frame.height == 2
    first = frame.row(0, named=True)
    assert first["call_index"] == 1
    assert first["called_at"] == datetime(2026, 8, 8, 11, 49, 40, 410000, tzinfo=UTC)
    assert first["message_uuid"] == "306c344f-4993-478a-b3a6-dea410e2a026"
    assert first["parent_uuid"] == "265394c6-8523-4349-b1be-234c38a25d9f"
    assert first["cache_read_tokens"] == 10090
    assert frame.row(1, named=True)["delta_seconds"] == 73


def test_csv_without_parent_uuid_still_loads(csv_without_parent: Path) -> None:
    frame = read_report_csv(_report(csv_without_parent))

    assert frame.height == 1
    assert frame.row(0, named=True)["parent_uuid"] is None


def test_blank_parent_uuid_becomes_null(tmp_path: Path) -> None:
    path = tmp_path / f"{SESSION_ID}_20260808T11:49:35.csv"
    path.write_text(
        "call,timestamp,uuid,parentUuid,input_tokens,cache_write_tokens,"
        "cache_read_tokens,output_tokens,delta_seconds\n"
        '1,"2026-08-08T11:49:40.410Z","306c344f-4993-478a-b3a6-dea410e2a026","",1,2,3,4,0\n',
        encoding="utf-8",
    )

    assert read_report_csv(_report(path)).row(0, named=True)["parent_uuid"] is None


def test_missing_columns_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / f"{SESSION_ID}_20260808T11:49:35.csv"
    path.write_text("call,timestamp\n1,x\n", encoding="utf-8")

    with pytest.raises(LoadError, match="missing columns"):
        read_report_csv(_report(path))
