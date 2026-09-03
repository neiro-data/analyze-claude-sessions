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
    actually differs (or there is no existing row at all). Never UPDATEs a
    priced row.
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
    skipped: list[str] = []
    for model_id, row in sorted(targets.items()):
        existing_row = conn.execute(
            """
            SELECT input_usd_per_mtok, output_usd_per_mtok,
                   cache_write_usd_per_mtok, cache_read_usd_per_mtok
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
        conn.execute(
            """
            INSERT INTO claude.model_pricing
                (model, effective_from, input_usd_per_mtok, output_usd_per_mtok,
                 cache_write_usd_per_mtok, cache_write_1h_usd_per_mtok,
                 cache_read_usd_per_mtok, retired, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (model, effective_from) DO NOTHING
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
            ),
        )
        inserted.append(model_id)
    conn.commit()

    db_models = {
        r["model"]
        for r in conn.execute(
            "SELECT DISTINCT model FROM claude.api_calls WHERE model IS NOT NULL"
        ).fetchall()
    }
    unmatched = tuple(sorted(db_models - set(targets) - {"<synthetic>"}))

    return SyncResult(
        inserted=tuple(inserted), skipped_unchanged=tuple(skipped), unmatched_db_models=unmatched
    )


def _matches(existing_row: Any, row: PriceRow) -> bool:
    from decimal import Decimal

    return (
        Decimal(existing_row["input_usd_per_mtok"]) == Decimal(row.base_input_usd_per_mtok)
        and Decimal(existing_row["output_usd_per_mtok"]) == Decimal(row.output_usd_per_mtok)
        and Decimal(existing_row["cache_write_usd_per_mtok"])
        == Decimal(row.cache_write_5m_usd_per_mtok)
        and Decimal(existing_row["cache_read_usd_per_mtok"]) == Decimal(row.cache_read_usd_per_mtok)
    )
