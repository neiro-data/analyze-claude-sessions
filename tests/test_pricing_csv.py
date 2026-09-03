"""Unit tests for the CSV-sourced pricing module: parsing, diffing, normalization.

No database needed -- these are pure functions over strings/dataclasses.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from analyze_claude_projects.pricing import (
    PricingParseError,
    diff_csv,
    is_retired,
    load_alias_map,
    normalize_model_id,
    parse_snapshot,
    read_csv,
    write_csv,
)

SNAPSHOT = """\
| Model | Base input tokens | 5m cache writes | 1h cache writes | Cache hits and refreshes | Output tokens |
| --- | --- | --- | --- | --- | --- |
| Claude Sonnet 5 | $2 / MTok | $2.50 / MTok | $4 / MTok | $0.20 / MTok | $10 / MTok |
| Claude Opus 5 | $5 / MTok | $6.25 / MTok | $10 / MTok | $0.50 / MTok | $25 / MTok |
| Claude Opus 4.1 (retired, except on Bedrock and Google Cloud) | $15 / MTok | $18.75 / MTok | $30 / MTok | $1.50 / MTok | $75 / MTok |
| Claude Haiku 4.5 | $1 / MTok | $1.25 / MTok | $2 / MTok | $0.10 / MTok | $5 / MTok |
| Claude Mythos 5.1 (limited availability) | $10 / MTok | $12.50 / MTok | $20 / MTok | $0.25 / MTok | $50 / MTok |
| Claude Sonnet 4.5 | $3 / MTok | $3.75 / MTok | $6 / MTok | $0.30 / MTok | $15 / MTok |
"""

SOURCE_URL = "https://platform.claude.com/docs/en/about-claude/pricing"


def test_normalize_model_id_strips_parenthetical_and_dots() -> None:
    assert normalize_model_id("Claude Opus 4.8") == "claude-opus-4-8"
    assert normalize_model_id("Claude Sonnet 5") == "claude-sonnet-5"
    assert (
        normalize_model_id("Claude Opus 4.1 (retired, except on Bedrock and Google Cloud)")
        == "claude-opus-4-1"
    )
    assert normalize_model_id("Claude Mythos 5.1 (limited availability)") == "claude-mythos-5-1"


def test_is_retired_detects_the_literal_substring() -> None:
    assert is_retired("Claude Opus 4 (retired, except on Google Cloud)")
    assert not is_retired("Claude Mythos 5 (limited availability)")
    assert not is_retired("Claude Sonnet 5")


def test_parse_snapshot_yields_expected_rows() -> None:
    rows = parse_snapshot(SNAPSHOT, ingested_date=date(2026, 9, 3), source_url=SOURCE_URL)
    assert len(rows) == 6
    by_model = {r.model: r for r in rows}
    assert by_model["claude-sonnet-5"].output_usd_per_mtok == "10"
    assert by_model["claude-opus-4-1"].retired is True
    assert by_model["claude-sonnet-5"].retired is False
    assert by_model["claude-mythos-5-1"].cache_read_usd_per_mtok == "0.25"


def test_parse_snapshot_rejects_too_few_rows() -> None:
    tiny = "\n".join(SNAPSHOT.splitlines()[:3])  # header + separator + 1 row
    with pytest.raises(PricingParseError):
        parse_snapshot(tiny, ingested_date=date(2026, 9, 3), source_url=SOURCE_URL)


def test_parse_snapshot_rejects_malformed_column_count() -> None:
    bad = "| Claude Sonnet 5 | $2 / MTok | $2.50 / MTok |\n" * 6
    with pytest.raises(PricingParseError):
        parse_snapshot(bad, ingested_date=date(2026, 9, 3), source_url=SOURCE_URL)


def test_diff_csv_identical_snapshot_is_a_no_op() -> None:
    rows = parse_snapshot(SNAPSHOT, ingested_date=date(2026, 9, 3), source_url=SOURCE_URL)
    merged, summary = diff_csv([], rows, as_of=date(2026, 9, 3))
    assert summary.new == 6
    assert summary.changed == 0

    # Re-running against the merged CSV with the same prices is a pure no-op.
    reparsed = parse_snapshot(SNAPSHOT, ingested_date=date(2026, 9, 10), source_url=SOURCE_URL)
    merged2, summary2 = diff_csv(merged, reparsed, as_of=date(2026, 9, 10))
    assert summary2.unchanged == 6
    assert summary2.changed == 0
    assert summary2.new == 0
    assert merged2 == merged  # byte-for-byte: no new rows appended


def test_diff_csv_appends_a_new_row_on_price_change() -> None:
    rows = parse_snapshot(SNAPSHOT, ingested_date=date(2026, 9, 3), source_url=SOURCE_URL)
    merged, _ = diff_csv([], rows, as_of=date(2026, 9, 3))

    changed_snapshot = SNAPSHOT.replace("$2 / MTok | $2.50", "$3 / MTok | $2.50")
    reparsed = parse_snapshot(
        changed_snapshot, ingested_date=date(2026, 9, 10), source_url=SOURCE_URL
    )
    merged2, summary = diff_csv(merged, reparsed, as_of=date(2026, 9, 10))

    assert summary.changed == 1
    assert summary.unchanged == 5
    sonnet_rows = [r for r in merged2 if r.model == "claude-sonnet-5"]
    assert len(sonnet_rows) == 2  # original 2026-09-03 row kept, new 2026-09-10 row appended
    assert sonnet_rows[-1].ingested_date == date(2026, 9, 10)
    assert sonnet_rows[-1].base_input_usd_per_mtok == "3"

    # Every other row's ingested_date is untouched.
    for row in merged2:
        if row.model != "claude-sonnet-5":
            assert row.ingested_date == date(2026, 9, 3)


def test_diff_csv_reports_delisted_models_without_dropping_them() -> None:
    rows = parse_snapshot(SNAPSHOT, ingested_date=date(2026, 9, 3), source_url=SOURCE_URL)
    merged, _ = diff_csv([], rows, as_of=date(2026, 9, 3))

    fewer = "\n".join(SNAPSHOT.splitlines()[:7])  # drop the last data row, keep 5 (>= MIN)
    reparsed = parse_snapshot(fewer, ingested_date=date(2026, 9, 10), source_url=SOURCE_URL)
    merged2, summary = diff_csv(merged, reparsed, as_of=date(2026, 9, 10))

    assert "claude-sonnet-4-5" in summary.delisted
    assert any(r.model == "claude-sonnet-4-5" for r in merged2)  # kept, not dropped


def test_csv_round_trip_is_byte_identical(tmp_path: Path) -> None:
    rows = parse_snapshot(SNAPSHOT, ingested_date=date(2026, 9, 3), source_url=SOURCE_URL)
    csv_path = tmp_path / "model_pricing.csv"
    write_csv(csv_path, rows)
    first = csv_path.read_bytes()

    reloaded = read_csv(csv_path)
    write_csv(csv_path, reloaded)
    second = csv_path.read_bytes()

    assert first == second


def test_load_alias_map_expands_snapshot_ids(tmp_path: Path) -> None:
    alias_path = tmp_path / "model_id_map.csv"
    alias_path.write_text("model,alias_for\nclaude-haiku-4-5-20251001,claude-haiku-4-5\n")
    aliases = load_alias_map(alias_path)
    assert aliases == {"claude-haiku-4-5-20251001": "claude-haiku-4-5"}


def test_load_alias_map_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_alias_map(tmp_path / "does-not-exist.csv") == {}


def test_tracked_pricing_csv_exists_and_parses() -> None:
    """data/reference/model_pricing.csv must exist, tracked, with the documented shape."""
    from analyze_claude_projects.config import REPO_ROOT

    csv_path = REPO_ROOT / "data" / "reference" / "model_pricing.csv"
    assert csv_path.exists(), f"{csv_path} is missing"
    rows = read_csv(csv_path)
    assert len(rows) == 17
    assert all(row.source_url == SOURCE_URL for row in rows)
    assert all(row.ingested_date == date(2026, 9, 3) for row in rows)
