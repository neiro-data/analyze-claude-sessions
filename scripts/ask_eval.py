"""Run the end-to-end `ask` eval (slots -> retrieval -> refusal) against the live catalog.

Reads the real `claude.nlp_examples` rows (`DATABASE_URL`), so `make up && make init-db` first.

Run:

    uv run --group nlp python scripts/ask_eval.py                    # tune + test tables
    uv run --group nlp python scripts/ask_eval.py --tune             # sweep tune.yaml only
    uv run --group nlp python scripts/ask_eval.py --update-baseline  # rewrite baseline.json

`--tune` prints the chosen threshold and never edits code. `--update-baseline` is for a
deliberate re-baseline (retrieval change, new model, catalog change); put the before/after
table in the PR.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer

from analyze_claude_sessions import nlp
from analyze_claude_sessions.ask_eval import (
    BASELINE_PATH,
    TEST_PATH,
    TUNE_PATH,
    build_baseline,
    default_retriever_factory,
    fetch_catalog,
    format_report,
    load_cases,
    metrics_by_kind,
    run_cases,
    tune,
)
from analyze_claude_sessions.config import get_settings
from analyze_claude_sessions.nlp import LocalEmbeddingBackend
from analyze_claude_sessions.nlp_stress import CONFIDENCE_SWEEP

app = typer.Typer(add_completion=False)


@app.command()
def main(
    do_tune: Annotated[
        bool, typer.Option("--tune", help="Sweep the confidence threshold on tune.yaml only.")
    ] = False,
    update_baseline: Annotated[
        bool, typer.Option("--update-baseline", help="Write baseline.json from the test split.")
    ] = False,
) -> None:
    examples = fetch_catalog(get_settings().database_url)
    backend = LocalEmbeddingBackend()

    if do_tune:
        rows, chosen = tune(examples, backend, CONFIDENCE_SWEEP)
        print(f"threshold sweep on {TUNE_PATH.name} (n={len(load_cases(TUNE_PATH))})")
        print("threshold  accuracy  wrong-accept  over-refusal")
        for r in rows:
            print(
                f"{r.threshold:9.2f}  {r.accuracy:8.3f}  {r.wrong_accept:12.3f}  "
                f"{r.over_refusal:12.3f}"
            )
        print(
            f"chosen: {chosen} (current CONFIDENCE_THRESHOLD={nlp.CONFIDENCE_THRESHOLD})"
            if chosen is not None
            else "no threshold meets the wrong-accept ceiling"
        )
        return

    factory = default_retriever_factory(backend)
    test_scored = run_cases(load_cases(TEST_PATH), examples, factory)
    if update_baseline:
        baseline = build_baseline(
            model_name=backend.model_name,
            examples=examples,
            threshold=nlp.CONFIDENCE_THRESHOLD,
            test=test_scored,
        )
        BASELINE_PATH.write_text(json.dumps(baseline, indent=2) + "\n")
        print(f"wrote {BASELINE_PATH}")

    tune_scored = run_cases(load_cases(TUNE_PATH), examples, factory)
    print(format_report("tune", metrics_by_kind(tune_scored)))
    print()
    print(format_report("test", metrics_by_kind(test_scored)))


if __name__ == "__main__":
    app()
