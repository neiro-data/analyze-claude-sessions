"""Drive scripts/cache_report_per_call_timing.sh and locate what it produced."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .discovery import TranscriptFile

# <jsonl stem>_YYYYMMDDTHH:MM:SS.csv - the stem is a session UUID for a main
# transcript and agent-<id> for a subagent one.
_CSV_NAME = re.compile(r"^(?P<stem>.+)_(?P<stamp>\d{8}T\d{2}:\d{2}:\d{2})\.csv$")
_OUTPUT_LINE = re.compile(r"CSV output written to:\s*(?P<path>.+?)\s*$", re.MULTILINE)
_BASH = shutil.which("bash") or "/bin/bash"

CSV_COLUMNS = (
    "call",
    "timestamp",
    "uuid",
    "input_tokens",
    "cache_write_tokens",
    "cache_read_tokens",
    "output_tokens",
    "delta_seconds",
    "delta_seconds_global",
    "agent",
    "agent_type",
    "depth",
    "parent_agent",
    "model",
    "tool_use_id",
    "description",
)


class ReportError(RuntimeError):
    """The report script failed or produced nothing we can find."""


@dataclass(frozen=True, slots=True)
class CsvReport:
    path: Path
    stem: str
    generated_at: datetime | None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    @property
    def size_bytes(self) -> int:
        return self.path.stat().st_size


def parse_csv_name(path: Path) -> tuple[str, datetime | None]:
    """Recover the source transcript stem and its first-record timestamp."""
    match = _CSV_NAME.match(path.name)
    if not match:
        raise ReportError(f"Unexpected report filename: {path.name}")
    stamp: datetime | None
    try:
        stamp = datetime.strptime(match["stamp"], "%Y%m%dT%H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        stamp = None
    return match["stem"], stamp


def output_dir_for(csv_dir: Path, transcript: TranscriptFile) -> Path:
    """CSVs are grouped per session so subagent filenames cannot collide."""
    return csv_dir / str(transcript.session_id)


def generate_report(script: Path, transcript: TranscriptFile, csv_dir: Path) -> CsvReport:
    """Run the shell report for one transcript and return the CSV it wrote."""
    if not script.is_file():
        raise ReportError(f"Report script not found: {script}")
    target_dir = output_dir_for(csv_dir, transcript)
    target_dir.mkdir(parents=True, exist_ok=True)

    completed = subprocess.run(  # noqa: S603 - resolved interpreter, no shell
        [
            _BASH,
            str(script),
            transcript.path.name,
            str(transcript.path.parent),
            str(target_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ReportError(
            f"{script.name} exited {completed.returncode} for {transcript.path.name}: "
            f"{completed.stderr.strip() or '<no stderr>'}"
        )

    match = _OUTPUT_LINE.search(completed.stderr)
    if not match:
        raise ReportError(f"Could not parse report output for {transcript.path.name}")

    produced = Path(match["path"])
    if not produced.is_absolute():
        produced = (target_dir / produced.name).resolve()
    if not produced.is_file():
        raise ReportError(f"Report claimed {produced} but the file is missing")

    stem, generated_at = parse_csv_name(produced)
    if stem != transcript.path.stem:
        raise ReportError(
            f"Report for {transcript.path.stem} was written as {stem} ({produced.name})"
        )
    return CsvReport(path=produced, stem=stem, generated_at=generated_at)
