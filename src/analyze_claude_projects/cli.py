"""`acp` command line interface."""

from __future__ import annotations

from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import get_settings
from .db import apply_migrations, connect, fetch_all, finish_run, start_run
from .discovery import discover_transcripts
from .parquet_export import DEFAULT_RELATIONS, ensure_extension, export_relation
from .pipeline import ingest_transcripts

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Analyse Claude Code transcripts in PostgreSQL.",
)
console = Console()

ProjectOption = Annotated[
    str | None,
    typer.Option("--project", "-p", help="Only sessions whose project folder contains this text."),
]
LimitOption = Annotated[int, typer.Option("--limit", "-n", min=1, help="Maximum rows to show.")]


def _shorten(value: str, limit: int = 44) -> str:
    """Keep the tail: project dir names share a long, uninformative prefix."""
    return value if len(value) <= limit else "…" + value[-(limit - 1) :]


def _render(rows: list[dict[str, Any]], title: str) -> None:
    if not rows:
        console.print(f"[yellow]{title}: no rows[/yellow]")
        return
    table = Table(title=title, header_style="bold cyan", row_styles=["", "dim"])
    for column in rows[0]:
        table.add_column(column, overflow="ellipsis", no_wrap=True)
    for row in rows:
        table.add_row(
            *(
                "" if value is None else _shorten(value) if isinstance(value, str) else str(value)
                for value in row.values()
            )
        )
    console.print(table)


@app.command()
def version() -> None:
    """Print the tool version and the resolved configuration."""
    settings = get_settings()
    console.print(f"analyze-claude-projects [bold]{__version__}[/bold]")
    console.print(f"  projects dir : {settings.projects_dir}")
    console.print(f"  csv dir      : {settings.csv_dir}")
    console.print(
        f"  parquet dir  : {settings.parquet_dir} (server sees {settings.parquet_dir_in_db})"
    )
    console.print(f"  report script: {settings.report_script}")


@app.command("init-db")
def init_db() -> None:
    """Apply sql/*.sql (extensions, schema, views). Idempotent."""
    settings = get_settings()
    with connect(settings.database_url) as conn:
        applied = apply_migrations(conn, settings.sql_dir)
        version_row = conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'pg_parquet'"
        ).fetchone()
    for name in applied:
        console.print(f"  applied {name}")
    extver = version_row["extversion"] if version_row else "MISSING"
    console.print(f"[green]Schema ready[/green] (pg_parquet {extver})")


@app.command()
def discover(project: ProjectOption = None) -> None:
    """List the transcripts that would be ingested."""
    settings = get_settings()
    transcripts = discover_transcripts(settings.projects_dir, project)
    rows = [
        {
            "project": item.project_dir,
            "session_id": str(item.session_id),
            "kind": item.kind,
            "agent_id": item.agent_id,
            "size_kb": f"{item.size_bytes / 1024:,.0f}",
        }
        for item in transcripts
    ]
    subagents = sum(1 for item in transcripts if item.is_subagent)
    _render(
        rows,
        f"{len(transcripts)} transcripts under {settings.projects_dir} "
        f"({len(transcripts) - subagents} sessions + {subagents} subagents)",
    )


@app.command()
def ingest(
    project: ProjectOption = None,
    force: Annotated[
        bool, typer.Option("--force", help="Re-report and reload transcripts that are unchanged.")
    ] = False,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Suppress per-session lines.")
    ] = False,
) -> None:
    """Run the full pipeline: discover -> CSV report -> PostgreSQL."""
    settings = get_settings()
    settings.ensure_dirs()
    transcripts = discover_transcripts(settings.projects_dir, project)
    console.print(f"Discovered [bold]{len(transcripts)}[/bold] transcripts")

    with connect(settings.database_url) as conn:
        run_id = start_run(
            conn,
            command="ingest" + (f" --project {project}" if project else ""),
            source_root=str(settings.projects_dir),
            csv_dir=str(settings.csv_dir),
        )
        hook = None if quiet else console.print
        summary = ingest_transcripts(
            conn, settings, transcripts, run_id=run_id, force=force, on_progress=hook
        )
        finish_run(
            conn,
            run_id,
            status="failed" if summary.failed and not summary.processed else "succeeded",
            discovered=summary.discovered,
            processed=summary.processed,
            skipped=summary.skipped,
            failed=summary.failed,
            rows_loaded=summary.rows_loaded,
            error_message="; ".join(f"{name}: {err}" for name, err in summary.failures[:20])
            or None,
        )

    console.print(
        f"[green]run {run_id}[/green] processed={summary.processed} "
        f"skipped={summary.skipped} failed={summary.failed} rows={summary.rows_loaded}"
    )
    if summary.failures:
        for name, error in summary.failures:
            console.print(f"  [red]{name}[/red] {error}")
        raise typer.Exit(code=1)


@app.command("export-parquet")
def export_parquet(
    relations: Annotated[
        list[str] | None,
        typer.Argument(help="Schema-qualified relations. Defaults to the core tables and views."),
    ] = None,
    compression: Annotated[str, typer.Option("--compression", "-c")] = "zstd",
) -> None:
    """Write relations to Parquet via pg_parquet's COPY TO."""
    settings = get_settings()
    targets = tuple(relations) if relations else DEFAULT_RELATIONS
    with connect(settings.database_url) as conn:
        console.print(f"pg_parquet {ensure_extension(conn)}")
        results = [
            export_relation(
                conn, relation, target_dir=settings.parquet_dir_in_db, compression=compression
            )
            for relation in targets
        ]
        conn.commit()
    _render(
        [{"relation": r.relation, "rows": r.row_count, "uri": r.uri} for r in results],
        f"Exported to {settings.parquet_dir}",
    )


@app.command()
def stats(limit: LimitOption = 15) -> None:
    """Show headline numbers plus the busiest projects and sessions."""
    settings = get_settings()
    with connect(settings.database_url) as conn:
        totals = fetch_all(
            conn,
            """
            SELECT (SELECT count(*) FROM claude.projects)    AS projects,
                   (SELECT count(*) FROM claude.sessions)    AS sessions,
                   (SELECT count(*) FROM claude.transcripts) AS transcripts,
                   (SELECT count(*) FROM claude.transcripts WHERE kind = 'subagent')
                       AS subagent_transcripts,
                   (SELECT count(*) FROM claude.api_calls)   AS api_calls,
                   (SELECT coalesce(sum(total_input_tokens), 0) FROM claude.api_calls)
                       AS total_input_tokens,
                   (SELECT coalesce(sum(output_tokens), 0) FROM claude.api_calls)
                       AS output_tokens,
                   (SELECT round(coalesce(sum(cache_read_tokens), 0)::numeric
                                 / NULLIF(sum(total_input_tokens), 0) * 100, 2)
                      FROM claude.api_calls) AS cache_read_pct
            """,
        )
        projects = fetch_all(
            conn,
            """
            SELECT project_dir, sessions, transcripts, api_calls, total_input_tokens,
                   output_tokens, cache_read_pct
              FROM claude.v_project_token_summary
             WHERE api_calls > 0
             ORDER BY total_input_tokens DESC NULLS LAST
             LIMIT %s
            """,
            (limit,),
        )
        sessions = fetch_all(
            conn,
            """
            SELECT project_dir, session_id, api_calls, subagent_api_calls,
                   total_input_tokens, output_tokens, cache_read_pct, avg_gap_seconds
              FROM claude.v_session_token_summary
             WHERE api_calls > 0
             ORDER BY total_input_tokens DESC NULLS LAST
             LIMIT %s
            """,
            (limit,),
        )
    _render(totals, "Totals")
    _render(projects, "Top projects by billed input tokens")
    _render(sessions, "Top sessions by billed input tokens")


@app.command()
def query(
    sql_text: Annotated[str, typer.Argument(help="A read-only SQL statement.")],
    limit: LimitOption = 50,
) -> None:
    """Run an ad-hoc SELECT against the analytics database."""
    settings = get_settings()
    if not sql_text.lstrip().lower().startswith(("select", "with", "table")):
        raise typer.BadParameter("Only SELECT / WITH / TABLE statements are allowed")
    with connect(settings.database_url) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = fetch_all(conn, sql_text)
    _render(rows[:limit], f"{len(rows)} rows")
