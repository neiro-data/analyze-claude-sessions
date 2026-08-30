from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from analyze_claude_projects.config import DEFAULT_REPORT_SCRIPT
from analyze_claude_projects.discovery import discover_transcripts
from analyze_claude_projects.report import ReportError, generate_report, parse_csv_name

from .conftest import SESSION_ID

needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")

EXPECTED_HEADER = (
    "call,timestamp,uuid,input_tokens,cache_write_tokens,cache_read_tokens,"
    "output_tokens,delta_seconds,delta_seconds_global,agent,agent_type,depth,"
    "parent_agent,model,tool_use_id,description"
)


def test_parse_csv_name_recovers_stem_and_stamp() -> None:
    stem, generated_at = parse_csv_name(Path(f"{SESSION_ID}_20260808T11:49:35.csv"))

    assert stem == str(SESSION_ID)
    assert generated_at == datetime(2026, 8, 8, 11, 49, 35, tzinfo=UTC)


def test_parse_csv_name_accepts_subagent_stems() -> None:
    stem, _ = parse_csv_name(Path("agent-a550490847ef81a58_20260808T11:49:35.csv"))

    assert stem == "agent-a550490847ef81a58"


def test_parse_csv_name_rejects_foreign_names() -> None:
    with pytest.raises(ReportError):
        parse_csv_name(Path("summary.csv"))


@needs_jq
def test_generate_report_matches_the_reference_layout(transcript: Path, tmp_path: Path) -> None:
    """End-to-end run of the vendored shell script."""
    item = discover_transcripts(transcript.parents[1])[0]

    report = generate_report(DEFAULT_REPORT_SCRIPT, item, tmp_path / "out")

    header, *rows = report.path.read_text(encoding="utf-8").strip().splitlines()
    assert header == EXPECTED_HEADER
    # Two requestId groups -> two rows; the second is 73s after the first.
    assert len(rows) == 2
    # delta_seconds,delta_seconds_global are the 8th/9th columns.
    assert rows[1].split(",")[7:9] == ["73", "73"]
    assert report.stem == str(SESSION_ID)
    assert report.generated_at == datetime(2026, 8, 8, 11, 49, 35, tzinfo=UTC)


@needs_jq
def test_reports_are_grouped_by_session(subagent_transcript: Path, tmp_path: Path) -> None:
    """Subagent CSVs land under the parent session so filenames cannot collide."""
    csv_dir = tmp_path / "out"
    found = discover_transcripts(subagent_transcript.parents[3])

    reports = [generate_report(DEFAULT_REPORT_SCRIPT, item, csv_dir) for item in found]

    assert all(report.path.parent == csv_dir / str(SESSION_ID) for report in reports)
    assert [report.stem for report in reports] == [
        str(SESSION_ID),
        "agent-a550490847ef81a58",
    ]


@needs_jq
def test_large_transcript_does_not_trip_sigpipe(transcript: Path, tmp_path: Path) -> None:
    """`jq | head -1` used to abort with exit 141 once the file was big enough."""
    body = transcript.read_text(encoding="utf-8")
    transcript.write_text(body * 200, encoding="utf-8")
    item = discover_transcripts(transcript.parents[1])[0]

    report = generate_report(DEFAULT_REPORT_SCRIPT, item, tmp_path / "out")

    assert report.path.read_text(encoding="utf-8").startswith(EXPECTED_HEADER)


def test_generate_report_reports_a_missing_script(transcript: Path, tmp_path: Path) -> None:
    item = discover_transcripts(transcript.parents[1])[0]

    with pytest.raises(ReportError, match="not found"):
        generate_report(tmp_path / "absent.sh", item, tmp_path)
