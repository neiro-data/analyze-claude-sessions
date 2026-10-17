"""Unit tests for the CSV-sourced pricing module: parsing, diffing, normalization.

No database needed -- these are pure functions over strings/dataclasses.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from analyze_claude_sessions.config import REPO_ROOT
from analyze_claude_sessions.pricing import (
    MIN_SNAPSHOT_ROWS,
    PriceRow,
    PricingParseError,
    _split_tier,
    diff_csv,
    is_retired,
    load_alias_map,
    normalize_model_id,
    parse_snapshot,
    read_csv,
    write_csv,
)
from analyze_claude_sessions.pricing_refresh import _matches

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

TIER_ROWS = """\
| Claude Haiku 5.5 (for prompts up to 100,000 tokens) | $0.10 / MTok | $0.125 / MTok | $0.20 / MTok | $0.01 / MTok | $0.50 / MTok |
| Claude Haiku 5.5 (for prompts over 100,000 tokens) | $0.50 / MTok | $0.625 / MTok | $1 / MTok | $0.05 / MTok | $2.50 / MTok |
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


def test_round_trip_with_tier_is_byte_identical(tmp_path: Path) -> None:
    rows = parse_snapshot(
        SNAPSHOT + TIER_ROWS, ingested_date=date(2026, 9, 3), source_url=SOURCE_URL
    )
    csv_path = tmp_path / "model_pricing.csv"
    write_csv(csv_path, rows)
    first = csv_path.read_bytes()
    reloaded = read_csv(csv_path)
    assert any(r.long_context_threshold_tokens == 100000 for r in reloaded)
    write_csv(csv_path, reloaded)
    assert first == csv_path.read_bytes()


def test_read_csv_legacy_header_has_no_tier(tmp_path: Path) -> None:
    csv_path = tmp_path / "legacy.csv"
    csv_path.write_text(
        "model,model_display_name,base_input_usd_per_mtok,cache_write_5m_usd_per_mtok,"
        "cache_write_1h_usd_per_mtok,cache_read_usd_per_mtok,output_usd_per_mtok,retired,"
        f"ingested_date,source_url\nclaude-x,Claude X,1,1.25,2,0.10,5,false,2026-09-03,{SOURCE_URL}\n"
    )
    (row,) = read_csv(csv_path)
    assert row.long_context_threshold_tokens is None
    assert row.long_context_output_usd_per_mtok is None


def test_parse_snapshot_merges_tier_pair() -> None:
    rows = parse_snapshot(
        SNAPSHOT + TIER_ROWS, ingested_date=date(2026, 10, 17), source_url=SOURCE_URL
    )
    haiku = [r for r in rows if r.model == "claude-haiku-5-5"]
    assert len(haiku) == 1
    (h,) = haiku
    assert h.model_display_name == "Claude Haiku 5.5"
    assert (h.base_input_usd_per_mtok, h.cache_write_5m_usd_per_mtok) == ("0.10", "0.125")
    assert (h.cache_write_1h_usd_per_mtok, h.cache_read_usd_per_mtok) == ("0.20", "0.01")
    assert h.output_usd_per_mtok == "0.50"
    assert h.long_context_threshold_tokens == 100000
    assert h.long_context_base_input_usd_per_mtok == "0.50"
    assert h.long_context_cache_write_5m_usd_per_mtok == "0.625"
    assert h.long_context_cache_write_1h_usd_per_mtok == "1"
    assert h.long_context_cache_read_usd_per_mtok == "0.05"
    assert h.long_context_output_usd_per_mtok == "2.50"
    for r in rows:
        if r.model != "claude-haiku-5-5":
            assert r.long_context_threshold_tokens is None
            assert r.long_context_output_usd_per_mtok is None


def test_parse_snapshot_rejects_half_tier_and_mismatched_threshold() -> None:
    over_only = TIER_ROWS.splitlines()[1] + "\n"
    with pytest.raises(PricingParseError):
        parse_snapshot(
            SNAPSHOT + over_only, ingested_date=date(2026, 10, 17), source_url=SOURCE_URL
        )
    mismatched = TIER_ROWS.replace("over 100,000", "over 200,000")
    with pytest.raises(PricingParseError):
        parse_snapshot(
            SNAPSHOT + mismatched, ingested_date=date(2026, 10, 17), source_url=SOURCE_URL
        )


def test_diff_csv_tier_unchanged_vs_changed() -> None:
    tiered = parse_snapshot(
        SNAPSHOT + TIER_ROWS, ingested_date=date(2026, 9, 3), source_url=SOURCE_URL
    )
    _, same = diff_csv(tiered, tiered, as_of=date(2026, 10, 17))
    assert same.changed == 0 and same.new == 0
    flat = [
        replace(
            r,
            long_context_threshold_tokens=None,
            long_context_base_input_usd_per_mtok=None,
            long_context_cache_write_5m_usd_per_mtok=None,
            long_context_cache_write_1h_usd_per_mtok=None,
            long_context_cache_read_usd_per_mtok=None,
            long_context_output_usd_per_mtok=None,
        )
        for r in tiered
    ]
    merged, changed = diff_csv(flat, tiered, as_of=date(2026, 10, 17))
    assert changed.changed == 1
    assert len(merged) == len(flat) + 1


def test_load_alias_map_expands_snapshot_ids(tmp_path: Path) -> None:
    alias_path = tmp_path / "model_id_map.csv"
    alias_path.write_text("model,alias_for\nclaude-haiku-4-5-20251001,claude-haiku-4-5\n")
    aliases = load_alias_map(alias_path)
    assert aliases == {"claude-haiku-4-5-20251001": "claude-haiku-4-5"}


def test_load_alias_map_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_alias_map(tmp_path / "does-not-exist.csv") == {}


def test_tracked_pricing_csv_exists_and_parses() -> None:
    """data/reference/model_pricing.csv must exist, tracked, with the documented shape."""
    csv_path = REPO_ROOT / "data" / "reference" / "model_pricing.csv"
    assert csv_path.exists(), f"{csv_path} is missing"
    rows = read_csv(csv_path)
    assert len(rows) == 20
    assert all(row.source_url == SOURCE_URL for row in rows)
    haiku = next(r for r in rows if r.model == "claude-haiku-5-5")
    assert haiku.long_context_threshold_tokens == 100000
    assert {row.ingested_date for row in rows} == {
        date(2026, 9, 3),
        date(2026, 9, 22),
        date(2026, 10, 17),
    }


def test_parse_snapshot_tolerates_extra_note_after_tier_label() -> None:
    noted = SNAPSHOT + TIER_ROWS.replace(
        "(for prompts over 100,000 tokens)", "(for prompts over 100,000 tokens) (beta)"
    )
    rows = parse_snapshot(noted, ingested_date=date(2026, 9, 3), source_url=SOURCE_URL)
    haiku = {r.model: r for r in rows}["claude-haiku-5-5"]
    assert haiku.long_context_threshold_tokens == 100000
    assert haiku.long_context_base_input_usd_per_mtok == "0.50"
    assert len(rows) == 7


def test_refresh_matches_compares_long_context_1h_cache_write() -> None:
    rows = parse_snapshot(
        SNAPSHOT + TIER_ROWS, ingested_date=date(2026, 9, 3), source_url=SOURCE_URL
    )
    haiku = {r.model: r for r in rows}["claude-haiku-5-5"]
    db = _db_row_for(haiku)
    assert _matches(db, haiku)
    assert not _matches({**db, "long_context_cache_write_1h_usd_per_mtok": "9"}, haiku)


def test_tier_label_with_several_trailing_notes_merges_to_base_model() -> None:
    noted = SNAPSHOT + (
        TIER_ROWS.replace(
            "(for prompts over 100,000 tokens)",
            "(for prompts over 100,000 tokens) (beta) (preview)",
        ).replace(
            "(for prompts up to 100,000 tokens)",
            "(for prompts up to 100,000 tokens) (beta) (preview)",
        )
    )
    rows = parse_snapshot(noted, ingested_date=date(2026, 9, 3), source_url=SOURCE_URL)
    haiku = [r for r in rows if r.model.startswith("claude-haiku-5")]
    assert [r.model for r in haiku] == ["claude-haiku-5-5"]
    assert haiku[0].model_display_name == "Claude Haiku 5.5"
    assert haiku[0].long_context_threshold_tokens == 100000


def test_non_tier_parenthetical_alone_is_not_a_tier() -> None:
    assert _split_tier("Claude Mythos 5.1 (limited availability)") == (
        "Claude Mythos 5.1 (limited availability)",
        None,
        None,
    )
    assert _split_tier("Claude Haiku 5.5 (beta)")[1:] == (None, None)
    assert _split_tier("Claude Haiku 5.5 (for prompts over 100,000 tokens) (beta)") == (
        "Claude Haiku 5.5",
        "over",
        100000,
    )


def _db_row_for(r: PriceRow) -> dict[str, object]:
    return {
        "input_usd_per_mtok": r.base_input_usd_per_mtok,
        "output_usd_per_mtok": r.output_usd_per_mtok,
        "cache_write_usd_per_mtok": r.cache_write_5m_usd_per_mtok,
        "cache_read_usd_per_mtok": r.cache_read_usd_per_mtok,
        "long_context_threshold_tokens": r.long_context_threshold_tokens,
        "long_context_input_usd_per_mtok": r.long_context_base_input_usd_per_mtok,
        "long_context_output_usd_per_mtok": r.long_context_output_usd_per_mtok,
        "long_context_cache_write_usd_per_mtok": r.long_context_cache_write_5m_usd_per_mtok,
        "long_context_cache_write_1h_usd_per_mtok": r.long_context_cache_write_1h_usd_per_mtok,
        "long_context_cache_read_usd_per_mtok": r.long_context_cache_read_usd_per_mtok,
    }


def test_matches_null_safety_on_long_context_1h_rate() -> None:
    rows = parse_snapshot(
        SNAPSHOT + TIER_ROWS, ingested_date=date(2026, 9, 3), source_url=SOURCE_URL
    )
    by_model = {r.model: r for r in rows}
    tiered = by_model["claude-haiku-5-5"]
    plain = by_model["claude-sonnet-5"]
    key = "long_context_cache_write_1h_usd_per_mtok"

    # both NULL -> match
    assert plain.long_context_cache_write_1h_usd_per_mtok is None
    assert _matches(_db_row_for(plain), plain)
    # NULL in DB vs value in CSV -> no match
    assert not _matches({**_db_row_for(tiered), key: None}, tiered)
    # value in DB vs NULL in CSV -> no match
    assert not _matches({**_db_row_for(plain), key: "1"}, plain)
    # numerically equal, differently formatted -> match
    assert _matches({**_db_row_for(tiered), key: "1.000"}, tiered)
    # differs only in the 1h long rate -> no match
    assert not _matches({**_db_row_for(tiered), key: "1.5"}, tiered)


def test_tracked_snapshots_reproduce_tracked_csv_without_diff() -> None:
    ref = REPO_ROOT / "data" / "reference"
    existing = read_csv(ref / "model_pricing.csv")
    # Snapshots can hold fewer rows than MIN_SNAPSHOT_ROWS; pad with throwaway models.
    pad = "".join(
        f"| Claude Pad {n} | $1 / MTok | $1 / MTok | $1 / MTok | $1 / MTok | $1 / MTok |\n"
        for n in range(MIN_SNAPSHOT_ROWS)
    )
    for snap in sorted((ref / "snapshots").glob("pricing_*.md")):
        snap_date = date.fromisoformat(snap.stem.removeprefix("pricing_"))
        parsed = [
            r
            for r in parse_snapshot(
                snap.read_text(encoding="utf-8").rstrip("\n") + "\n" + pad,
                ingested_date=snap_date,
                source_url=SOURCE_URL,
            )
            if not r.model.startswith("claude-pad-")
        ]
        assert parsed
        merged, _ = diff_csv(existing, parsed, as_of=snap_date)
        assert merged == existing, f"{snap.name} no longer reproduces the tracked CSV"
