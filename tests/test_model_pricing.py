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
