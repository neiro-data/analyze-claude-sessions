"""`acp` command line interface."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated, Any

import psycopg
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__
from .config import REPO_ROOT, Settings, get_settings
from .db import apply_migrations, connect, fetch_all, finish_run, start_run
from .discovery import discover_transcripts
from .parquet_export import DEFAULT_RELATIONS, ensure_extension, export_relation
from .pipeline import ingest_transcripts
from .pricing_refresh import DEFAULT_SOURCE_URL, refresh, sync_db

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Analyse Claude Code transcripts in PostgreSQL.",
)
pricing_app = typer.Typer(
    add_completion=False, no_args_is_help=True, help="CSV-sourced model pricing."
)
app.add_typer(pricing_app, name="pricing")
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
    console.print(f"analyze-claude-sessions [bold]{__version__}[/bold]")
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
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Also print per-session lines with no API calls."),
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

        def _print(msg: str) -> None:
            if not verbose and ("(no API calls)" in msg or "(unchanged)" in msg):
                return
            console.print(msg)

        hook = None if quiet else _print
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


def ask_role_privileged(conn: psycopg.Connection[Any]) -> str | None:
    """Return why the connected role is too privileged for `ask`, else None.

    Flags a superuser or the owner of the `claude` schema (the owner-role
    fallback when ASK_DATABASE_URL is unset). Read-only catalog query, so it
    also works for `ask_reader`.
    """
    rows = fetch_all(
        conn,
        "SELECT r.rolname, r.rolsuper, n.nspowner = r.oid AS owns_claude "
        "FROM pg_roles r CROSS JOIN pg_namespace n "
        "WHERE r.rolname = current_user AND n.nspname = 'claude'",
    )
    if not rows:
        return None
    row = rows[0]
    if row["rolsuper"]:
        return f"superuser role {row['rolname']}"
    if row["owns_claude"]:
        return f"owner role {row['rolname']}"
    return None


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="A natural-language question about the data.")],
    top_k: Annotated[int, typer.Option("--top-k", min=1, help="Candidates to consider.")] = 3,
    show_sql: Annotated[bool, typer.Option("--sql", help="Print the matched SQL.")] = False,
    execute: Annotated[bool, typer.Option(help="Run the matched query.")] = True,
    rerank: Annotated[
        bool, typer.Option("--rerank/--no-rerank", help="Cross-encoder re-rank of the top hits.")
    ] = False,
    contrast_guard: Annotated[
        bool,
        typer.Option(
            "--contrast-guard/--no-contrast-guard",
            help="Demote catalog rows that say the opposite (least/most, read/write, ...).",
        ),
    ] = True,
    llm: Annotated[
        bool,
        typer.Option("--llm", help="Opt in: let Claude write the SQL (needs the 'llm' group)."),
    ] = False,
    summarize: Annotated[
        bool,
        typer.Option(
            "--summarize",
            help="With --llm: LLM summary; sends up to 50 result rows to the Anthropic API.",
        ),
    ] = False,
) -> None:
    """Answer a question by matching it to curated SQL and running it read-only.

    Fully offline: retrieval uses a local sentence-transformers model, no
    network access or API key is required (one-time `uv sync --group nlp`).
    """
    # Imported lazily so the optional `nlp` dependency group (numpy, sentence
    # -transformers) is only required by this command, not the whole CLI.
    try:
        from .nlp import (
            MAX_LIMIT,
            AskDecision,
            InvalidSlotError,
            LocalEmbeddingBackend,
            NlpDependencyError,
            UnboundFilterError,
            UnsafeQueryError,
            ensure_read_only,
            hybrid_retriever_factory,
            load_examples,
            resolve_ask,
        )
    except ImportError as exc:
        console.print(
            "[red]Natural-language queries need the optional 'nlp' dependency "
            "group.[/red] Install it with [bold]uv sync --group nlp[/bold] "
            "(or rebuild the app image)."
        )
        raise typer.Exit(code=1) from exc

    settings = get_settings()
    if llm:
        _ask_llm(question, settings, execute=execute, summarize=summarize)
        return
    decision: AskDecision | None = None
    try:
        with connect(settings.ask_database_url) as conn:
            privileged = ask_role_privileged(conn)
            if privileged:
                console.print(
                    f"[dim]Running as {privileged}; set ASK_DATABASE_URL to the read-only "
                    "ask_reader role (see the read-only role in the README).[/dim]"
                )
            rows = fetch_all(
                conn,
                "SELECT example_id, question, sql_text, description, tags, slots, paraphrases "
                "FROM claude.nlp_examples",
            )
            examples = load_examples(rows)
            known_projects = [
                r["dir_name"]
                for r in fetch_all(conn, "SELECT DISTINCT dir_name FROM claude.projects")
            ]
            known_models = [
                r["model"]
                for r in fetch_all(
                    conn, "SELECT DISTINCT model FROM claude.api_calls WHERE model IS NOT NULL"
                )
            ]
            decision = resolve_ask(
                question,
                examples,
                hybrid_retriever_factory(
                    LocalEmbeddingBackend(), rerank=rerank, contrast_guard=contrast_guard
                ),
                known_projects=known_projects,
                known_models=known_models,
                top_k=top_k,
            )

            if decision.outcome == "no_examples":
                if "session_id" in decision.slots:
                    console.print("[yellow]No session-scoped queries available yet.[/yellow]")
                else:
                    console.print(
                        "[yellow]No curated questions available yet.[/yellow] "
                        "Provide a session_id=<id> to run session-scoped queries."
                    )
                raise typer.Exit(code=1)

            if decision.outcome == "low_confidence":
                console.print(
                    f"[yellow]Not confident enough (best score {decision.score:.2f}).[/yellow] "
                    "Try rephrasing, or pick one of these:"
                )
                for example, score in decision.ranked:
                    console.print(f"  [{score:.2f}] {example.question}")
                raise typer.Exit(code=1)

            top_example = decision.example
            if top_example is None:  # unreachable: only "no_examples" lacks one
                raise typer.Exit(code=1)
            if decision.outcome == "unbindable":
                raise UnboundFilterError(list(decision.unbindable))
            if decision.slots.get("limit", 0) > MAX_LIMIT:
                console.print(f"[dim]Showing top {MAX_LIMIT}.[/dim]")

            console.print(
                f"[green]Matched:[/green] {top_example.question} "
                f"[dim](score {decision.score:.2f})[/dim]"
            )
            if show_sql:
                console.print(f"[dim]{top_example.sql_text}[/dim]")
            if not execute:
                return

            ensure_read_only(top_example.sql_text)
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '15s'")
            result_rows = fetch_all(conn, top_example.sql_text, decision.params)
        _render(result_rows, top_example.question)
    except UnboundFilterError as exc:
        # Raised either for an extracted filter the match can't bind (`decision`
        # is set) or by `resolve_ask` for a required slot with no value.
        if decision is None:
            console.print(
                f"[red]Refusing:[/red] the matched query needs {', '.join(exc.unbindable)} "
                "but your question doesn't give one."
            )
            raise typer.Exit(code=1) from exc
        filters = ", ".join(f"{name} ({decision.slots.get(name)!r})" for name in exc.unbindable)
        matched = decision.example.question if decision.example else "?"
        console.print(
            f"[red]Refusing:[/red] your question filters by {filters} but the closest "
            f'match "{matched}" has no such filter. It does not run with the '
            "filter silently dropped. Try one of these instead:"
        )
        for example, score in decision.ranked:
            console.print(f"  [{score:.2f}] {example.question}")
        raise typer.Exit(code=1) from exc
    except NlpDependencyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    except InvalidSlotError as exc:
        console.print(f"[red]Refusing:[/red] {exc}.")
        raise typer.Exit(code=1) from exc
    except UnsafeQueryError as exc:
        console.print(f"[red]Refusing to run query: {exc}[/red]")
        raise typer.Exit(code=1) from exc
    except psycopg.Error as exc:
        # A bad session_id (e.g. a value that fails the uuid cast) or any other
        # database error surfaces as a friendly message, not a raw traceback.
        console.print(f"[red]Query failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc


def _ask_llm(question: str, settings: Settings, *, execute: bool, summarize: bool) -> None:
    """`acp ask --llm`: retrieve examples + schema, let Claude write SQL, run it as ask_reader."""
    from . import llm as llm_mod
    from .nlp import (
        LocalEmbeddingBackend,
        NlpDependencyError,
        UnsafeQueryError,
        ensure_read_only,
        hybrid_retriever_factory,
        load_examples,
    )

    if not settings.ask_database_url_explicit:
        console.print(
            "[red]--llm needs ASK_DATABASE_URL set to the read-only ask_reader role[/red] "
            "(no owner-role fallback in this mode)."
        )
        raise typer.Exit(code=1)
    if not settings.anthropic_api_key_set:
        console.print("[red]--llm needs ANTHROPIC_API_KEY in the environment (.env).[/red]")
        raise typer.Exit(code=1)
    try:
        client = llm_mod.make_client()
        # Phase 1: read-only context fetch. The connection closes before the API call so no
        # transaction idles (or holds a snapshot) while Claude generates.
        with connect(settings.ask_database_url) as conn:
            # Read-only and the timeout must be the first statements of the transaction.
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '15s'")
            privileged = ask_role_privileged(conn)
            if privileged:
                console.print(
                    f"[red]--llm refuses to run as {privileged}[/red]: "
                    "point ASK_DATABASE_URL at the ask_reader role."
                )
                raise typer.Exit(code=1)
            examples = load_examples(
                fetch_all(
                    conn,
                    "SELECT example_id, question, sql_text, description, tags, slots, paraphrases "
                    "FROM claude.nlp_examples",
                )
            )
            ranked = hybrid_retriever_factory(LocalEmbeddingBackend())(examples).search(
                question, top_k=llm_mod.EXAMPLES_K
            )
            schema_rows = fetch_all(
                conn,
                "SELECT object_name, object_type, column_name, data_type, column_comment "
                "FROM claude.v_schema_catalog",
            )
        context = llm_mod.build_context(question, ranked, schema_rows)
        sql = llm_mod.generate_sql(client, settings.ask_llm_model, question, context)
        console.print(f"[green]Generated SQL[/green] [dim]({settings.ask_llm_model})[/dim]")
        console.print(f"[dim]{escape(sql)}[/dim]")
        if not execute:
            return
        ensure_read_only(sql)
        # Phase 2: run it in a fresh read-only transaction.
        with connect(settings.ask_database_url) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '15s'")
            # No params: fetch_all would pass () and psycopg then rejects a literal `%`.
            rows = [dict(r) for r in conn.execute(sql).fetchall()]
        _render(rows, question)
        if summarize and rows:
            console.print(escape(llm_mod.summarize(client, settings.ask_llm_model, question, rows)))
    except (llm_mod.LlmError, NlpDependencyError) as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from exc
    except UnsafeQueryError as exc:
        console.print(f"[red]Refusing to run generated query: {escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from exc
    except psycopg.Error as exc:
        console.print(f"[red]Query failed:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc


DEFAULT_PRICING_CSV = REPO_ROOT / "data" / "reference" / "model_pricing.csv"
DEFAULT_ALIAS_MAP = REPO_ROOT / "data" / "reference" / "model_id_map.csv"


@pricing_app.command("refresh")
def pricing_refresh_cmd(
    input_path: Annotated[
        Path, typer.Option("--input", exists=True, help="Snapshot file to parse.")
    ],
    as_of: Annotated[
        str | None,
        typer.Option("--as-of", help="ISO date to stamp new rows with (default: today)."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the diff, write nothing.")
    ] = False,
) -> None:
    """Diff a fetched pricing snapshot against data/reference/model_pricing.csv."""
    as_of_date = date.fromisoformat(as_of) if as_of else None
    result = refresh(DEFAULT_PRICING_CSV, input_path=input_path, as_of=as_of_date, dry_run=dry_run)
    s = result.summary
    console.print(f"{s.unchanged} unchanged, {s.changed} changed, {s.new} new")
    if s.delisted:
        console.print(
            f"[yellow]Delisted (kept in CSV, no longer on the page):[/yellow] "
            f"{', '.join(s.delisted)}"
        )
    if dry_run:
        console.print("[dim]--dry-run: nothing written[/dim]")
    elif result.wrote:
        console.print(f"[green]Wrote[/green] {DEFAULT_PRICING_CSV}")


@pricing_app.command("sync-db")
def pricing_sync_db_cmd() -> None:
    """Load data/reference/model_pricing.csv into claude.model_pricing (append-only)."""
    settings = get_settings()
    with connect(settings.database_url) as conn:
        result = sync_db(
            conn, DEFAULT_PRICING_CSV, DEFAULT_ALIAS_MAP, source_url=DEFAULT_SOURCE_URL
        )
    if result.inserted:
        names = ", ".join(result.inserted)
        console.print(f"[green]Inserted[/green] {len(result.inserted)}: {names}")
    else:
        console.print("[dim]No new rows to insert[/dim]")
    if result.tier_filled:
        console.print(
            f"[green]Filled long-context tier[/green] {len(result.tier_filled)}: "
            f"{', '.join(result.tier_filled)}"
        )
    if result.tier_drift:
        console.print(
            f"[yellow]Warning:[/yellow] long-context tier differs from CSV, left unchanged "
            f"(a tier change needs a new effective_from): {', '.join(result.tier_drift)}"
        )
    console.print(f"{len(result.skipped_unchanged)} unchanged, skipped")
    if result.unmatched_db_models:
        console.print(
            f"[yellow]Warning:[/yellow] DB models with no CSV price: "
            f"{', '.join(result.unmatched_db_models)}"
        )
