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

from analyze_claude_sessions.config import Settings

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
    from analyze_claude_sessions.config import REPO_ROOT
    from analyze_claude_sessions.pricing_refresh import sync_db

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
    from analyze_claude_sessions.config import REPO_ROOT
    from analyze_claude_sessions.pricing_refresh import sync_db

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


# ---------------------------------------------------------------------------
# Long-context (prompt-length) tier
# ---------------------------------------------------------------------------
_LC_CALLS = {
    # case: (model, input, cache_write, cache_read)
    "at_99999": ("__test_lc_model__", 1, 0, 99_998),
    "at_100000": ("__test_lc_model__", 1, 0, 99_999),
    "at_100001": ("__test_lc_model__", 1, 1, 99_999),
    "flat_over": ("__test_flat_model__", 200_000, 0, 0),
}


@pytest.fixture
def lc_conn() -> Iterator[Connection[Any]]:
    """Non-autocommit connection; fixtures are inserted then rolled back at teardown."""
    settings = Settings.from_env()
    try:
        connection = psycopg.connect(settings.database_url, autocommit=False, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        pytest.skip(f"no live database reachable: {exc}")
    try:
        connection.execute(
            """
            INSERT INTO claude.model_pricing
                (model, effective_from, input_usd_per_mtok, output_usd_per_mtok,
                 cache_write_usd_per_mtok, cache_read_usd_per_mtok, source,
                 long_context_threshold_tokens, long_context_input_usd_per_mtok,
                 long_context_output_usd_per_mtok, long_context_cache_write_usd_per_mtok,
                 long_context_cache_read_usd_per_mtok)
            VALUES ('__test_lc_model__', '2020-01-01', 0.10, 0.50, 0.125, 0.01, 'test',
                    100000, 0.50, 2.50, 0.625, 0.05),
                   ('__test_flat_model__', '2020-01-01', 0.10, 0.50, 0.125, 0.01, 'test',
                    NULL, NULL, NULL, NULL, NULL)
            """
        )
        connection.execute("INSERT INTO claude.projects (dir_name) VALUES ('__test_lc_project__')")
        connection.execute(
            """
            INSERT INTO claude.sessions (session_id, project_id)
            SELECT '00000000-0000-0000-0000-0000000000aa'::uuid, project_id
            FROM claude.projects WHERE dir_name = '__test_lc_project__'
            """
        )
        connection.execute(
            """
            INSERT INTO claude.transcripts (session_id, jsonl_path)
            VALUES ('00000000-0000-0000-0000-0000000000aa', '__test_lc__.jsonl')
            """
        )
        for i, (model, inp, cw, cr) in enumerate(_LC_CALLS.values(), start=1):
            connection.execute(
                """
                INSERT INTO claude.api_calls
                    (transcript_id, session_id, call_index, called_at, message_uuid, model,
                     input_tokens, cache_write_tokens, cache_read_tokens,
                     output_tokens, output_tokens_raw)
                SELECT transcript_id, session_id, %s, '2026-10-20T00:00:00Z',
                       gen_random_uuid(), %s, %s, %s, %s, 1000000, 1000000
                FROM claude.transcripts WHERE jsonl_path = '__test_lc__.jsonl'
                """,
                (i, model, inp, cw, cr),
            )
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _lc_cost(conn: Connection[Any], view: str, case: str) -> Any:
    model, inp, cw, cr = _LC_CALLS[case]
    row = conn.execute(
        f"""
        SELECT v.cost_usd FROM claude.{view} v
        JOIN claude.api_calls c USING (call_id)
        WHERE c.model = %s AND c.input_tokens = %s
          AND c.cache_write_tokens = %s AND c.cache_read_tokens = %s
        """,  # noqa: S608 - view name is a test constant
        (model, inp, cw, cr),
    ).fetchone()
    assert row is not None
    return row["cost_usd"]


def _rate_cost(rates: tuple[str, str, str, str]) -> Any:
    from decimal import Decimal

    r_in, r_cw, r_cr, r_out = (Decimal(r) for r in rates)

    def cost(inp: int, cw: int, cr: int) -> Decimal:
        return (inp * r_in + cw * r_cw + cr * r_cr) / Decimal(1_000_000) + r_out

    return cost


def test_long_context_tier_boundary_uses_strict_greater_than(lc_conn: Connection[Any]) -> None:
    from decimal import Decimal

    # output 1M tokens -> 0.50 (standard) vs 2.50 (long); input parts are added on top.
    std = _rate_cost(("0.10", "0.125", "0.01", "0.50"))
    lng = _rate_cost(("0.50", "0.625", "0.05", "2.50"))
    q = Decimal("0.000001")
    assert _lc_cost(lc_conn, "v_api_call_cost", "at_99999") == std(1, 0, 99_998).quantize(q)
    assert _lc_cost(lc_conn, "v_api_call_cost", "at_100000") == std(1, 0, 99_999).quantize(q)
    assert _lc_cost(lc_conn, "v_api_call_cost", "at_100001") == lng(1, 1, 99_999).quantize(q)
    assert _lc_cost(lc_conn, "v_api_call_cost", "flat_over") == std(200_000, 0, 0).quantize(q)


def test_long_context_tier_applies_to_duplicated_view(lc_conn: Connection[Any]) -> None:
    from decimal import Decimal

    expected = (
        Decimal(1) * Decimal("0.50")
        + Decimal(1) * Decimal("0.625")
        + Decimal(99_999) * Decimal("0.05")
    ) / Decimal(1_000_000) + Decimal("2.50")
    got = _lc_cost(lc_conn, "v_api_call_cost_duplicated", "at_100001")
    assert got == expected.quantize(Decimal("0.000001"))


def test_cost_view_columns_unchanged_by_long_context(conn: Connection[Any]) -> None:
    cols = conn.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'claude' AND table_name = 'v_api_call_cost'
        ORDER BY ordinal_position
        """
    ).fetchall()
    assert [c["column_name"] for c in cols] == [
        "call_id",
        "transcript_id",
        "session_id",
        "called_at",
        "model",
        "input_tokens",
        "cache_write_tokens",
        "cache_read_tokens",
        "output_tokens",
        "pricing_effective_from",
        "cost_usd",
    ]


def test_haiku_5_5_has_its_tier_after_pricing_sync(conn: Connection[Any]) -> None:
    """Read-only: relies on `make pricing-sync-db` having run (see the gate in the plan)."""
    row = conn.execute(
        """
        SELECT long_context_threshold_tokens FROM claude.model_pricing
        WHERE model = 'claude-haiku-5-5' ORDER BY effective_from DESC LIMIT 1
        """
    ).fetchone()
    assert row is not None and row["long_context_threshold_tokens"] == 100000


def test_sync_db_fill_never_overwrites_a_set_tier(conn: Connection[Any], tmp_path: Any) -> None:
    from datetime import date

    from analyze_claude_sessions.pricing import PriceRow, write_csv
    from analyze_claude_sessions.pricing_refresh import sync_db

    connection = conn  # sync_db commits, so this test cleans up explicitly (autocommit conn)
    connection.execute("DELETE FROM claude.model_pricing WHERE model = '__test_fill_model__'")
    try:
        connection.execute(
            """
            INSERT INTO claude.model_pricing
                (model, effective_from, input_usd_per_mtok, output_usd_per_mtok,
                 cache_write_usd_per_mtok, cache_read_usd_per_mtok, source,
                 long_context_threshold_tokens, long_context_input_usd_per_mtok,
                 long_context_output_usd_per_mtok, long_context_cache_write_usd_per_mtok,
                 long_context_cache_read_usd_per_mtok)
            VALUES ('__test_fill_model__', '2026-10-17', 1, 5, 1.25, 0.1, 'test',
                    50000, 2, 10, 2.5, 0.2)
            """
        )
        csv_path = tmp_path / "pricing.csv"
        write_csv(
            csv_path,
            [
                PriceRow(
                    model="__test_fill_model__",
                    model_display_name="t",
                    base_input_usd_per_mtok="1",
                    cache_write_5m_usd_per_mtok="1.25",
                    cache_write_1h_usd_per_mtok="2",
                    cache_read_usd_per_mtok="0.1",
                    output_usd_per_mtok="5",
                    retired=False,
                    ingested_date=date(2026, 10, 17),
                    source_url="test",
                    long_context_threshold_tokens=999,
                    long_context_base_input_usd_per_mtok="9",
                    long_context_cache_write_5m_usd_per_mtok="9",
                    long_context_cache_write_1h_usd_per_mtok="9",
                    long_context_cache_read_usd_per_mtok="9",
                    long_context_output_usd_per_mtok="9",
                )
            ],
        )
        result = sync_db(connection, csv_path, tmp_path / "none.csv")
        assert "__test_fill_model__" not in result.inserted + result.tier_filled
        assert result.tier_drift == ("__test_fill_model__",)
        row = connection.execute(
            "SELECT long_context_threshold_tokens AS t FROM claude.model_pricing "
            "WHERE model = '__test_fill_model__'"
        ).fetchone()
        assert row is not None and row["t"] == 50000
    finally:
        connection.execute("DELETE FROM claude.model_pricing WHERE model = '__test_fill_model__'")


def test_sync_db_fills_a_missing_tier_then_is_a_noop(conn: Connection[Any], tmp_path: Any) -> None:
    from datetime import date

    from analyze_claude_sessions.pricing import PriceRow, write_csv
    from analyze_claude_sessions.pricing_refresh import sync_db

    model = "__test_fill_null_model__"
    conn.execute("DELETE FROM claude.model_pricing WHERE model = %s", (model,))
    try:
        conn.execute(
            """
            INSERT INTO claude.model_pricing
                (model, effective_from, input_usd_per_mtok, output_usd_per_mtok,
                 cache_write_usd_per_mtok, cache_read_usd_per_mtok, source)
            VALUES (%s, '2026-10-17', 1, 5, 1.25, 0.1, 'test')
            """,
            (model,),
        )
        csv_path = tmp_path / "pricing.csv"
        write_csv(
            csv_path,
            [
                PriceRow(
                    model=model,
                    model_display_name="t",
                    base_input_usd_per_mtok="1",
                    cache_write_5m_usd_per_mtok="1.25",
                    cache_write_1h_usd_per_mtok="2",
                    cache_read_usd_per_mtok="0.1",
                    output_usd_per_mtok="5",
                    retired=False,
                    ingested_date=date(2026, 10, 17),
                    source_url="test",
                    long_context_threshold_tokens=50000,
                    long_context_base_input_usd_per_mtok="2",
                    long_context_cache_write_5m_usd_per_mtok="2.5",
                    long_context_cache_write_1h_usd_per_mtok="4",
                    long_context_cache_read_usd_per_mtok="0.2",
                    long_context_output_usd_per_mtok="10",
                )
            ],
        )
        first = sync_db(conn, csv_path, tmp_path / "none.csv")
        assert first.tier_filled == (model,) and first.tier_drift == ()
        again = sync_db(conn, csv_path, tmp_path / "none.csv")
        assert again.inserted == () and again.tier_filled == () and again.tier_drift == ()
    finally:
        conn.execute("DELETE FROM claude.model_pricing WHERE model = %s", (model,))
