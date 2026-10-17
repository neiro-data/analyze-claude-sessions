"""Orchestration for `acp pricing refresh` / `acp pricing sync-db`.

Refresh: snapshot file -> parse -> diff -> append-only CSV write.
Sync-db: CSV (+ alias expansion) -> append-only INSERT into
`claude.model_pricing`, never an UPDATE against a priced row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from psycopg import Connection

from .pricing import (
    DiffSummary,
    PriceRow,
    diff_csv,
    load_alias_map,
    parse_snapshot,
    read_csv,
    write_csv,
)

DEFAULT_SOURCE_URL = "https://platform.claude.com/docs/en/about-claude/pricing"


@dataclass(frozen=True, slots=True)
class RefreshResult:
    summary: DiffSummary
    wrote: bool


def refresh(
    csv_path: Path,
    *,
    input_path: Path,
    as_of: date | None = None,
    source_url: str = DEFAULT_SOURCE_URL,
    dry_run: bool = False,
) -> RefreshResult:
    """Parse `input_path`, diff against `csv_path`, and (unless dry-run) write it."""
    text = input_path.read_text(encoding="utf-8")
    parsed = parse_snapshot(text, ingested_date=as_of or date.today(), source_url=source_url)
    existing = read_csv(csv_path)
    merged, summary = diff_csv(existing, parsed, as_of=as_of or date.today())
    wrote = summary.has_writes and not dry_run
    if wrote:
        write_csv(csv_path, merged)
    return RefreshResult(summary=summary, wrote=wrote)


@dataclass(frozen=True, slots=True)
class SyncResult:
    inserted: tuple[str, ...]
    skipped_unchanged: tuple[str, ...]
    unmatched_db_models: tuple[str, ...]
    tier_filled: tuple[str, ...] = ()
    tier_drift: tuple[str, ...] = ()


def sync_db(
    conn: Connection[Any],
    csv_path: Path,
    alias_map_path: Path,
    *,
    source_url: str = DEFAULT_SOURCE_URL,
) -> SyncResult:
    """Load the CSV's latest-per-model rows (+ alias expansion) into `claude.model_pricing`.

    Append-only: for each target model id, compares against the newest
    existing DB row and inserts a new `effective_from` row only when a price
    actually differs (or there is no existing row at all). Never overwrites a
    standard rate; the only UPDATE is a fill-only one that sets a long-context
    tier on an existing same-`effective_from` row whose tier is still NULL.
    """
    rows = read_csv(csv_path)
    latest_by_model: dict[str, PriceRow] = {}
    for row in rows:
        current = latest_by_model.get(row.model)
        if current is None or row.ingested_date > current.ingested_date:
            latest_by_model[row.model] = row

    aliases = load_alias_map(alias_map_path)
    targets: dict[str, PriceRow] = dict(latest_by_model)
    for alias_id, base_id in aliases.items():
        if base_id in latest_by_model:
            targets[alias_id] = latest_by_model[base_id]

    inserted: list[str] = []
    tier_filled: list[str] = []
    tier_drift: list[str] = []
    skipped: list[str] = []
    for model_id, row in sorted(targets.items()):
        existing_row = conn.execute(
            """
            SELECT input_usd_per_mtok, output_usd_per_mtok,
                   cache_write_usd_per_mtok, cache_read_usd_per_mtok,
                   long_context_threshold_tokens, long_context_input_usd_per_mtok,
                   long_context_output_usd_per_mtok, long_context_cache_write_usd_per_mtok,
                   long_context_cache_write_1h_usd_per_mtok, long_context_cache_read_usd_per_mtok
              FROM claude.model_pricing
             WHERE model = %s
             ORDER BY effective_from DESC
             LIMIT 1
            """,
            (model_id,),
        ).fetchone()
        if existing_row is not None and _matches(existing_row, row):
            skipped.append(model_id)
            continue
        # Fill-only ON CONFLICT: set threshold + NULL long 1h rate reports tier_drift;
        # fix that row with a manual UPDATE.
        result = conn.execute(
            """
            INSERT INTO claude.model_pricing
                (model, effective_from, input_usd_per_mtok, output_usd_per_mtok,
                 cache_write_usd_per_mtok, cache_write_1h_usd_per_mtok,
                 cache_read_usd_per_mtok, retired, source,
                 long_context_threshold_tokens, long_context_input_usd_per_mtok,
                 long_context_output_usd_per_mtok, long_context_cache_write_usd_per_mtok,
                 long_context_cache_write_1h_usd_per_mtok, long_context_cache_read_usd_per_mtok)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (model, effective_from) DO UPDATE SET
                long_context_threshold_tokens = EXCLUDED.long_context_threshold_tokens,
                long_context_input_usd_per_mtok = EXCLUDED.long_context_input_usd_per_mtok,
                long_context_output_usd_per_mtok = EXCLUDED.long_context_output_usd_per_mtok,
                long_context_cache_write_usd_per_mtok =
                    EXCLUDED.long_context_cache_write_usd_per_mtok,
                long_context_cache_write_1h_usd_per_mtok =
                    EXCLUDED.long_context_cache_write_1h_usd_per_mtok,
                long_context_cache_read_usd_per_mtok =
                    EXCLUDED.long_context_cache_read_usd_per_mtok
            WHERE claude.model_pricing.long_context_threshold_tokens IS NULL
              AND EXCLUDED.long_context_threshold_tokens IS NOT NULL
            RETURNING (xmax = 0) AS inserted
            """,
            (
                model_id,
                row.ingested_date,
                row.base_input_usd_per_mtok,
                row.output_usd_per_mtok,
                row.cache_write_5m_usd_per_mtok,
                row.cache_write_1h_usd_per_mtok,
                row.cache_read_usd_per_mtok,
                row.retired,
                f"{source_url}, ingested {row.ingested_date.isoformat()}",
                row.long_context_threshold_tokens,
                row.long_context_base_input_usd_per_mtok,
                row.long_context_output_usd_per_mtok,
                row.long_context_cache_write_5m_usd_per_mtok,
                row.long_context_cache_write_1h_usd_per_mtok,
                row.long_context_cache_read_usd_per_mtok,
            ),
        ).fetchone()
        if result is None:
            tier_drift.append(model_id)  # same effective_from, tier differs: left untouched
            continue
        (inserted if result["inserted"] else tier_filled).append(model_id)
    conn.commit()

    db_models = {
        r["model"]
        for r in conn.execute(
            "SELECT DISTINCT model FROM claude.api_calls WHERE model IS NOT NULL"
        ).fetchall()
    }
    unmatched = tuple(sorted(db_models - set(targets) - {"<synthetic>"}))

    return SyncResult(
        inserted=tuple(inserted),
        skipped_unchanged=tuple(skipped),
        unmatched_db_models=unmatched,
        tier_filled=tuple(tier_filled),
        tier_drift=tuple(tier_drift),
    )


def _matches(existing_row: Any, row: PriceRow) -> bool:
    from decimal import Decimal

    def same(db_value: Any, csv_value: str | int | None) -> bool:
        if db_value is None or csv_value is None:
            return db_value is None and csv_value is None
        return Decimal(db_value) == Decimal(csv_value)

    return (
        Decimal(existing_row["input_usd_per_mtok"]) == Decimal(row.base_input_usd_per_mtok)
        and Decimal(existing_row["output_usd_per_mtok"]) == Decimal(row.output_usd_per_mtok)
        and Decimal(existing_row["cache_write_usd_per_mtok"])
        == Decimal(row.cache_write_5m_usd_per_mtok)
        and Decimal(existing_row["cache_read_usd_per_mtok"]) == Decimal(row.cache_read_usd_per_mtok)
        and same(existing_row["long_context_threshold_tokens"], row.long_context_threshold_tokens)
        and same(
            existing_row["long_context_input_usd_per_mtok"],
            row.long_context_base_input_usd_per_mtok,
        )
        and same(
            existing_row["long_context_output_usd_per_mtok"], row.long_context_output_usd_per_mtok
        )
        and same(
            existing_row["long_context_cache_write_usd_per_mtok"],
            row.long_context_cache_write_5m_usd_per_mtok,
        )
        and same(
            existing_row["long_context_cache_write_1h_usd_per_mtok"],
            row.long_context_cache_write_1h_usd_per_mtok,
        )
        and same(
            existing_row["long_context_cache_read_usd_per_mtok"],
            row.long_context_cache_read_usd_per_mtok,
        )
    )
