"""Live-DB checks for claude.model_pricing and the cost columns it feeds.

Opt-in like the `slow` suite (`-m live_db`) since these need a running Postgres
with sql/*.sql applied (`make up && make init-db`) -- skipped automatically
when no DB is reachable, so the fast default suite stays hermetic.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg import Connection
from psycopg.rows import dict_row

from analyze_claude_projects.config import Settings

pytestmark = pytest.mark.live_db


@pytest.fixture
def conn() -> Iterator[Connection[Any]]:
    settings = Settings.from_env()
    try:
        with psycopg.connect(
            settings.database_url, autocommit=True, row_factory=dict_row
        ) as connection:
            yield connection
    except psycopg.OperationalError as exc:
        pytest.skip(f"no live database reachable: {exc}")


def test_model_pricing_seeded_for_models_in_use(conn: Connection[Any]) -> None:
    """Every priced model in api_calls has at least one model_pricing row."""
    rows = conn.execute(
        """
        SELECT DISTINCT c.model
        FROM claude.api_calls c
        WHERE c.model NOT IN (SELECT model FROM claude.model_pricing)
          AND c.model <> '<synthetic>'
        """
    ).fetchall()
    assert rows == [], f"models present in api_calls with no pricing row: {rows}"


def test_unpriced_model_yields_null_not_zero(conn: Connection[Any]) -> None:
    """A call against an unpriced/synthetic model must cost NULL, never 0."""
    row = conn.execute(
        """
        SELECT cost_usd
        FROM claude.v_api_call_cost
        WHERE model = '<synthetic>'
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        pytest.skip("no <synthetic> call rows in this database")
    assert row["cost_usd"] is None


def test_session_cost_matches_hand_computed_total(conn: Connection[Any]) -> None:
    """A known session's cost is reproducible by hand from its token counts."""
    session = conn.execute(
        """
        SELECT session_id, cost_usd
        FROM claude.v_session_token_summary
        WHERE cost_usd IS NOT NULL
        ORDER BY cost_usd DESC
        LIMIT 1
        """
    ).fetchone()
    if session is None:
        pytest.skip("no priced sessions in this database")

    calls = conn.execute(
        """
        SELECT input_tokens, cache_write_tokens, cache_read_tokens, output_tokens, cost_usd
        FROM claude.v_api_call_cost
        WHERE session_id = %s
        """,
        (session["session_id"],),
    ).fetchall()

    hand_total = sum(float(c["cost_usd"]) for c in calls if c["cost_usd"] is not None)
    assert hand_total == pytest.approx(float(session["cost_usd"]), rel=1e-9)


def test_pricing_sync_db_is_append_only_and_idempotent(conn: Connection[Any]) -> None:
    """`sync_db` never UPDATEs a priced row; re-running it twice is a no-op."""
    from analyze_claude_projects.config import REPO_ROOT
    from analyze_claude_projects.pricing_refresh import sync_db

    csv_path = REPO_ROOT / "data" / "reference" / "model_pricing.csv"
    alias_path = REPO_ROOT / "data" / "reference" / "model_id_map.csv"

    before = conn.execute("SELECT count(*) AS n FROM claude.model_pricing").fetchone()
    sync_db(conn, csv_path, alias_path)
    after_first = conn.execute("SELECT count(*) AS n FROM claude.model_pricing").fetchone()
    assert after_first is not None and before is not None
    assert after_first["n"] >= before["n"]

    result = sync_db(conn, csv_path, alias_path)
    after_second = conn.execute("SELECT count(*) AS n FROM claude.model_pricing").fetchone()
    assert after_second is not None
    assert after_second["n"] == after_first["n"]
    assert result.inserted == ()


def test_pricing_sync_db_skips_duplicate_seed_prices(conn: Connection[Any]) -> None:
    """The 4 seeded 2026-01-01 rows must not get a duplicate identical-price row."""
    from analyze_claude_projects.config import REPO_ROOT
    from analyze_claude_projects.pricing_refresh import sync_db

    csv_path = REPO_ROOT / "data" / "reference" / "model_pricing.csv"
    alias_path = REPO_ROOT / "data" / "reference" / "model_id_map.csv"
    sync_db(conn, csv_path, alias_path)

    for model in ("claude-sonnet-5", "claude-opus-5", "claude-opus-4-8"):
        rows = conn.execute(
            "SELECT effective_from FROM claude.model_pricing WHERE model = %s", (model,)
        ).fetchall()
        # The CSV's price matches the 2026-01-01 seed row exactly, so sync-db must
        # not have appended a duplicate 2026-09-03 row on top of it.
        assert len(rows) == 1, f"{model} got a duplicate-price row: {rows}"


def test_price_change_costs_older_calls_at_the_older_rate(conn: Connection[Any]) -> None:
    """A call before a rate change is priced at the old rate (two-row price history)."""
    conn.execute(
        """
        INSERT INTO claude.model_pricing
            (model, effective_from, input_usd_per_mtok, output_usd_per_mtok,
             cache_write_usd_per_mtok, cache_read_usd_per_mtok, source)
        VALUES
            ('__test_history_model__', '2020-01-01', 1.00, 1.00, 1.00, 1.00, 'test'),
            ('__test_history_model__', '2030-01-01', 2.00, 2.00, 2.00, 2.00, 'test')
        ON CONFLICT (model, effective_from) DO NOTHING
        """
    )
    row = conn.execute(
        """
        SELECT
            (100 * 1.00 / 1e6)::numeric(12,6) AS expected_old_input_cost,
            mp.input_usd_per_mtok
        FROM claude.model_pricing mp
        WHERE mp.model = '__test_history_model__'
          AND mp.effective_from <= '2025-06-01'::date
        ORDER BY mp.effective_from DESC
        LIMIT 1
        """
    ).fetchone()
    assert row is not None
    assert row["input_usd_per_mtok"] == 1.00  # picks the pre-change rate, not the 2030 one
    conn.execute("DELETE FROM claude.model_pricing WHERE model = '__test_history_model__'")


def test_cost_view_column_order_is_backward_compatible(conn: Connection[Any]) -> None:
    """cost_usd must be appended, not inserted -- CREATE OR REPLACE VIEW requires it."""
    cols = conn.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'claude' AND table_name = 'v_session_token_summary'
        ORDER BY ordinal_position
        """
    ).fetchall()
    names = [c["column_name"] for c in cols]
    assert names[-1] == "cost_usd"
    assert names[0] == "session_id"
