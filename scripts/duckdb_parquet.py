"""Ad-hoc charts over `data/parquet/` with the stack down -- no Postgres needed.

DuckDB's `read_parquet` scans the exported files directly, so this works even
when `make down` has stopped the containers; it's the fastest path from
"I want a quick number" to an answer without spinning PostgreSQL back up.

Run:

    uv run python scripts/duckdb_parquet.py                 # example aggregations
    uv run python scripts/duckdb_parquet.py --sql "SELECT ..."  # your own query

Or open a DuckDB shell directly:

    uv run python -c "import duckdb; duckdb.sql(\"SELECT * FROM \
        read_parquet('data/parquet/api_calls.parquet') LIMIT 5\").show()"
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import duckdb
import typer

app = typer.Typer(add_completion=False)

DEFAULT_PARQUET_DIR = Path("data/parquet")

EXAMPLE_QUERIES = {
    "tokens_by_model": """
        SELECT
            model,
            count(*)                                   AS api_calls,
            sum(input_tokens)                           AS input_tokens,
            sum(cache_write_tokens)                      AS cache_write_tokens,
            sum(cache_read_tokens)                       AS cache_read_tokens,
            sum(output_tokens)                           AS output_tokens
        FROM read_parquet('{dir}/api_calls.parquet')
        GROUP BY model
        ORDER BY api_calls DESC
    """,
    "calls_by_session": """
        SELECT
            session_id,
            count(*)                                   AS api_calls,
            sum(total_input_tokens)                      AS total_input_tokens,
            sum(output_tokens)                           AS output_tokens
        FROM read_parquet('{dir}/api_calls.parquet')
        GROUP BY session_id
        ORDER BY api_calls DESC
        LIMIT 20
    """,
}


@app.command()
def main(
    parquet_dir: Annotated[
        Path, typer.Option(help="Directory holding the pg_parquet exports.")
    ] = DEFAULT_PARQUET_DIR,
    sql: Annotated[
        str | None, typer.Option(help="Run this query instead of the example set.")
    ] = None,
) -> None:
    """Open `data/parquet/*.parquet` with DuckDB and run example aggregations."""
    if not parquet_dir.exists():
        raise typer.BadParameter(f"{parquet_dir} does not exist -- run `make export-parquet` first.")

    con = duckdb.connect()

    if sql:
        con.sql(sql).show()
        return

    for name, query in EXAMPLE_QUERIES.items():
        typer.echo(f"\n=== {name} ===")
        con.sql(query.format(dir=parquet_dir)).show()


if __name__ == "__main__":
    app()
