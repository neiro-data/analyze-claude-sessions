"""CSV-sourced model pricing: parsing, diffing, and model-id normalization.

`data/reference/model_pricing.csv` is the human-auditable source of truth for
Claude API list prices (see README.md). This module is pure I/O-adjacent
logic -- no database access -- so it can be unit tested without Postgres.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path

CSV_FIELDS = (
    "model",
    "model_display_name",
    "base_input_usd_per_mtok",
    "cache_write_5m_usd_per_mtok",
    "cache_write_1h_usd_per_mtok",
    "cache_read_usd_per_mtok",
    "output_usd_per_mtok",
    "retired",
    "ingested_date",
    "source_url",
    "long_context_threshold_tokens",
    "long_context_base_input_usd_per_mtok",
    "long_context_cache_write_5m_usd_per_mtok",
    "long_context_cache_write_1h_usd_per_mtok",
    "long_context_cache_read_usd_per_mtok",
    "long_context_output_usd_per_mtok",
)

_PRICE_FIELDS = (
    "base_input_usd_per_mtok",
    "cache_write_5m_usd_per_mtok",
    "cache_write_1h_usd_per_mtok",
    "cache_read_usd_per_mtok",
    "output_usd_per_mtok",
)

# Optional long-context (prompt-length) tier rates, all text, None when absent.
_LONG_PRICE_FIELDS = (
    "long_context_base_input_usd_per_mtok",
    "long_context_cache_write_5m_usd_per_mtok",
    "long_context_cache_write_1h_usd_per_mtok",
    "long_context_cache_read_usd_per_mtok",
    "long_context_output_usd_per_mtok",
)

_TIER_RE = re.compile(
    r"\(for prompts (up to|over) ([\d,]+) tokens\)(?:\s*\([^)]*\))*\s*$", re.IGNORECASE
)

MIN_SNAPSHOT_ROWS = 5


class PricingParseError(ValueError):
    """The pricing snapshot could not be parsed with confidence."""


@dataclass(frozen=True, slots=True)
class PriceRow:
    """One CSV row. Prices are kept as the original decimal-string text (never re-rounded)."""

    model: str
    model_display_name: str
    base_input_usd_per_mtok: str
    cache_write_5m_usd_per_mtok: str
    cache_write_1h_usd_per_mtok: str
    cache_read_usd_per_mtok: str
    output_usd_per_mtok: str
    retired: bool
    ingested_date: date
    source_url: str
    long_context_threshold_tokens: int | None = None
    long_context_base_input_usd_per_mtok: str | None = None
    long_context_cache_write_5m_usd_per_mtok: str | None = None
    long_context_cache_write_1h_usd_per_mtok: str | None = None
    long_context_cache_read_usd_per_mtok: str | None = None
    long_context_output_usd_per_mtok: str | None = None

    def as_csv_dict(self) -> dict[str, str]:
        return {
            "model": self.model,
            "model_display_name": self.model_display_name,
            "base_input_usd_per_mtok": self.base_input_usd_per_mtok,
            "cache_write_5m_usd_per_mtok": self.cache_write_5m_usd_per_mtok,
            "cache_write_1h_usd_per_mtok": self.cache_write_1h_usd_per_mtok,
            "cache_read_usd_per_mtok": self.cache_read_usd_per_mtok,
            "output_usd_per_mtok": self.output_usd_per_mtok,
            "retired": "true" if self.retired else "false",
            "ingested_date": self.ingested_date.isoformat(),
            "source_url": self.source_url,
            "long_context_threshold_tokens": (
                ""
                if self.long_context_threshold_tokens is None
                else str(self.long_context_threshold_tokens)
            ),
            **{f: getattr(self, f) or "" for f in _LONG_PRICE_FIELDS},
        }

    def prices_equal(self, other: PriceRow) -> bool:
        """Numeric equality on prices + retired flag, ignoring text formatting."""
        if self.retired != other.retired:
            return False
        if self.long_context_threshold_tokens != other.long_context_threshold_tokens:
            return False
        if not all(
            Decimal(getattr(self, field)) == Decimal(getattr(other, field))
            for field in _PRICE_FIELDS
        ):
            return False
        for field in _LONG_PRICE_FIELDS:
            mine, theirs = getattr(self, field), getattr(other, field)
            if (mine is None) != (theirs is None):
                return False
            if mine is not None and Decimal(mine) != Decimal(theirs):
                return False
        return True


@dataclass(frozen=True, slots=True)
class DiffSummary:
    unchanged: int
    changed: int
    new: int
    delisted: tuple[str, ...]

    @property
    def has_writes(self) -> bool:
        return self.changed > 0 or self.new > 0


def normalize_model_id(display_name: str) -> str:
    """`Claude Opus 4.1 (retired, ...)` -> `claude-opus-4-1`.

    Rule: strip a trailing parenthetical (retirement/availability notes),
    lowercase, spaces and dots become hyphens.
    """
    base = re.sub(r"\s*\([^)]*\)\s*$", "", display_name).strip()
    return base.lower().replace(".", "-").replace(" ", "-")


def is_retired(display_name: str) -> bool:
    return "(retired" in display_name


def load_alias_map(path: Path) -> dict[str, str]:
    """Read `model_id_map.csv` (columns: model, alias_for) -> {snapshot_id: base_model_id}."""
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as fh:
        return {row["model"]: row["alias_for"] for row in csv.DictReader(fh)}


def read_csv(path: Path) -> list[PriceRow]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return [
            PriceRow(
                model=row["model"],
                model_display_name=row["model_display_name"],
                base_input_usd_per_mtok=row["base_input_usd_per_mtok"],
                cache_write_5m_usd_per_mtok=row["cache_write_5m_usd_per_mtok"],
                cache_write_1h_usd_per_mtok=row["cache_write_1h_usd_per_mtok"],
                cache_read_usd_per_mtok=row["cache_read_usd_per_mtok"],
                output_usd_per_mtok=row["output_usd_per_mtok"],
                retired=row["retired"].strip().lower() == "true",
                ingested_date=date.fromisoformat(row["ingested_date"]),
                source_url=row["source_url"],
                long_context_threshold_tokens=(
                    int(row["long_context_threshold_tokens"])
                    if row.get("long_context_threshold_tokens")
                    else None
                ),
                **{f: row.get(f) or None for f in _LONG_PRICE_FIELDS},
            )
            for row in reader
        ]


def write_csv(path: Path, rows: list[PriceRow]) -> None:
    """Write atomically (temp file + os.replace), sorted by (model, ingested_date)."""
    ordered = sorted(rows, key=lambda r: (r.model, r.ingested_date))
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        for row in ordered:
            writer.writerow(row.as_csv_dict())
    os.replace(tmp_path, path)


def parse_snapshot(text: str, *, ingested_date: date, source_url: str) -> list[PriceRow]:
    """Parse the markdown "Model pricing" table into `PriceRow`s.

    Hard-fails (raises `PricingParseError`) rather than silently returning an
    empty/shrunken table -- a parser drift on the docs page must be loud.
    """
    rows: list[PriceRow] = []
    tiers: list[tuple[PriceRow, str, int]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) != 6:
            continue
        if cells[0].lower() in ("model", "---") or set(cells[0]) <= {"-", " "}:
            continue
        if not re.search(r"claude", cells[0], re.IGNORECASE):
            continue
        display_name = cells[0]
        try:
            prices = [_parse_money(cell) for cell in cells[1:]]
        except ValueError as exc:
            raise PricingParseError(f"Unparseable price cell in row {cells!r}: {exc}") from exc
        base_name, kind, threshold = _split_tier(display_name)
        price_row = PriceRow(
            model=normalize_model_id(base_name),
            model_display_name=base_name,
            base_input_usd_per_mtok=prices[0],
            cache_write_5m_usd_per_mtok=prices[1],
            cache_write_1h_usd_per_mtok=prices[2],
            cache_read_usd_per_mtok=prices[3],
            output_usd_per_mtok=prices[4],
            retired=is_retired(display_name),
            ingested_date=ingested_date,
            source_url=source_url,
        )
        if kind is None or threshold is None:
            rows.append(price_row)
        else:
            tiers.append((price_row, kind, threshold))
    rows.extend(_merge_tiers(tiers))
    if len(rows) < MIN_SNAPSHOT_ROWS:
        raise PricingParseError(
            f"Parsed only {len(rows)} pricing row(s), expected at least {MIN_SNAPSHOT_ROWS} -- "
            "refusing to treat this as a valid snapshot."
        )
    return rows


def _split_tier(display_name: str) -> tuple[str, str | None, int | None]:
    """`Claude X (for prompts over 100,000 tokens)` -> (`Claude X`, `over`, 100000)."""
    match = _TIER_RE.search(display_name)
    if not match:
        return display_name, None, None
    base = display_name[: match.start()].strip()
    return base, match.group(1).lower().replace(" ", "_"), int(match.group(2).replace(",", ""))


def _merge_tiers(tiers: list[tuple[PriceRow, str, int]]) -> list[PriceRow]:
    """Merge "up to N" / "over N" row pairs into one row carrying the long-context tier."""
    by_model: dict[str, dict[str, tuple[PriceRow, int]]] = {}
    for row, kind, threshold in tiers:
        slot = by_model.setdefault(row.model, {})
        if kind in slot:
            raise PricingParseError(f"Duplicate '{kind}' tier row for {row.model}")
        slot[kind] = (row, threshold)
    merged: list[PriceRow] = []
    for model, slot in by_model.items():
        if "up_to" not in slot or "over" not in slot:
            raise PricingParseError(
                f"Tiered pricing for {model} needs both 'up to' and 'over' rows"
            )
        (std, up_n), (lng, over_n) = slot["up_to"], slot["over"]
        if up_n != over_n:
            raise PricingParseError(
                f"Tier thresholds for {model} differ: up to {up_n} vs over {over_n}"
            )
        merged.append(
            replace(
                std,
                long_context_threshold_tokens=up_n,
                long_context_base_input_usd_per_mtok=lng.base_input_usd_per_mtok,
                long_context_cache_write_5m_usd_per_mtok=lng.cache_write_5m_usd_per_mtok,
                long_context_cache_write_1h_usd_per_mtok=lng.cache_write_1h_usd_per_mtok,
                long_context_cache_read_usd_per_mtok=lng.cache_read_usd_per_mtok,
                long_context_output_usd_per_mtok=lng.output_usd_per_mtok,
            )
        )
    return merged


def _parse_money(cell: str) -> str:
    """`$12.50 / MTok` -> `12.50`. Raises ValueError if it doesn't look like a price."""
    match = re.search(r"\$\s*([0-9]+(?:\.[0-9]+)?)", cell)
    if not match:
        raise ValueError(cell)
    return match.group(1)


def diff_csv(
    existing: list[PriceRow], parsed: list[PriceRow], *, as_of: date
) -> tuple[list[PriceRow], DiffSummary]:
    """Diff a freshly parsed snapshot against the CSV's latest row per model.

    Append-only: an unchanged price is left untouched (row + ingested_date
    preserved); a changed price appends a new row dated `as_of`; a model
    absent from the CSV is appended as new; a model absent from the snapshot
    but present in the CSV is left alone and reported as delisted.
    """
    latest_by_model: dict[str, PriceRow] = {}
    for row in existing:
        current = latest_by_model.get(row.model)
        if current is None or row.ingested_date > current.ingested_date:
            latest_by_model[row.model] = row

    result = list(existing)
    unchanged = changed = new = 0
    seen_models: set[str] = set()

    for row in parsed:
        seen_models.add(row.model)
        latest = latest_by_model.get(row.model)
        if latest is None:
            result.append(replace(row, ingested_date=as_of))
            new += 1
        elif latest.prices_equal(row):
            unchanged += 1
        else:
            result.append(replace(row, ingested_date=as_of))
            changed += 1

    delisted = tuple(sorted(set(latest_by_model) - seen_models))
    return result, DiffSummary(unchanged=unchanged, changed=changed, new=new, delisted=delisted)
