"""Run the real-model RAG stress battery and write a timestamped report.

Reuses `analyze_claude_sessions.nlp_stress` (perturbations, `FakeHashBackend`,
`SQL_ADVERSARIAL_CORPUS`, `generate_slot_questions`) and the real
`CatalogRetriever` / `LocalEmbeddingBackend` from `analyze_claude_sessions.nlp`.
Mirrors the measurements in `tests/test_nlp_stress_slow.py`, packaged as a
standalone, runnable report generator rather than a test.

Run:

    uv run --group nlp python scripts/rag_stress.py            # full battery
    uv run --group nlp python scripts/rag_stress.py --quick    # small/fast sanity run

Writes `data/reports/rag_stress_<timestamp>.md` and `.csv`.
"""

from __future__ import annotations

import csv
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from analyze_claude_sessions.nlp import (
    DEFAULT_MODEL_NAME,
    CatalogRetriever,
    LocalEmbeddingBackend,
    UnsafeQueryError,
    ensure_read_only,
    extract_slots,
)
from analyze_claude_sessions.nlp_stress import (
    CONFIDENCE_SWEEP,
    PERTURBATIONS,
    SQL_ADVERSARIAL_CORPUS,
    FakeHashBackend,
    build_catalog_examples,
    generate_slot_questions,
    load_yaml,
    paraphrase_pairs,
    percentiles,
    score_pairs,
    synthetic_catalog,
)

app = typer.Typer(add_completion=False)

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_PATH = REPO_ROOT / "tests" / "data" / "nlp_eval.yaml"
STRESS_PATH = REPO_ROOT / "tests" / "data" / "nlp_stress.yaml"
DEFAULT_REPORTS_DIR = REPO_ROOT / "data" / "reports"

# Derived, never hardcoded: the report must name the model actually used.
MODEL_NAME = DEFAULT_MODEL_NAME


@dataclass
class Report:
    seed: int
    timestamp: str
    catalog_size: int
    baseline: tuple[float, float, float]
    perturbation_results: dict[str, tuple[float, float, float]]
    threshold_sweep: list[tuple[float, float, float]]
    false_accept_rate: float
    guard_bypasses: list[tuple[str, str, str]]
    known_bypasses: list[tuple[str, str, str]]
    slot_fuzz_failures: list[str]
    latency_by_size: dict[str, tuple[float, float, float]]  # label -> (build, p50, p95)


def run_battery(*, seed: int, quick: bool) -> Report:
    eval_data = load_yaml(EVAL_PATH)
    stress_data = load_yaml(STRESS_PATH)

    catalog_questions = list(eval_data["catalog"].keys())
    examples = build_catalog_examples(catalog_questions)
    retriever = CatalogRetriever(examples, LocalEmbeddingBackend())

    pairs = paraphrase_pairs(eval_data)
    baseline = score_pairs(retriever, pairs)

    perturbation_results: dict[str, tuple[float, float, float]] = {}
    classes = list(PERTURBATIONS.items())
    if quick:
        classes = classes[:3]
    for name, perturbation in classes:
        rng = random.Random(seed)  # noqa: S311
        perturbed_pairs = [(cq, perturbation(q, rng)) for cq, q in pairs]
        perturbation_results[name] = score_pairs(retriever, perturbed_pairs)

    out_of_catalog = [item["question"] for item in stress_data["out_of_catalog"]]
    threshold_sweep = []
    in_catalog_scores: list[tuple[float, bool]] = []
    for catalog_question, query in pairs:
        ranked = retriever.search(query, top_k=1)
        if ranked:
            in_catalog_scores.append((ranked[0][1], ranked[0][0].question == catalog_question))
    out_of_catalog_ranked = [retriever.search(q, top_k=1) for q in out_of_catalog]
    out_of_catalog_scores = [ranked[0][1] if ranked else -1.0 for ranked in out_of_catalog_ranked]
    for threshold in CONFIDENCE_SWEEP:
        accepted_correct = sum(
            1 for score, correct in in_catalog_scores if score >= threshold and correct
        )
        accept_rate = accepted_correct / len(in_catalog_scores)
        false_accepts = sum(1 for score in out_of_catalog_scores if score >= threshold)
        false_accept_rate = false_accepts / len(out_of_catalog_scores)
        threshold_sweep.append((threshold, accept_rate, false_accept_rate))

    false_accepts_default = sum(
        1 for ranked in out_of_catalog_ranked if ranked and retriever.is_confident(ranked[0][1])
    )
    false_accept_rate = false_accepts_default / len(out_of_catalog)

    guard_bypasses = []
    known_bypasses: list[tuple[str, str, str]] = []
    for case in SQL_ADVERSARIAL_CORPUS:
        try:
            ensure_read_only(case.sql)
            allowed = True
        except UnsafeQueryError:
            allowed = False
        if allowed != case.expect_allow:
            guard_bypasses.append((case.case_id, case.sql, case.note))
        if case.known_bypass and allowed:
            known_bypasses.append((case.case_id, case.sql, case.note))

    slot_fuzz_failures = []
    rng = random.Random(seed)  # noqa: S311
    slot_count = 20 if quick else 200
    for case in generate_slot_questions(rng, count=slot_count):
        try:
            extract_slots(
                case.question,
                known_projects=list(case.known_projects),
                known_models=list(case.known_models),
                today=case.today,
            )
        except Exception as exc:  # noqa: BLE001 - fuzz target
            slot_fuzz_failures.append(f"{case.question!r} raised {exc!r}")

    latency_by_size: dict[str, tuple[float, float, float]] = {}
    sizes = [100, 1_000] if quick else [100, 1_000, 10_000]
    for size in sizes:
        questions = synthetic_catalog(size, seed)
        fake_examples = build_catalog_examples(questions)
        build_start = time.perf_counter()
        fake_retriever = CatalogRetriever(fake_examples, FakeHashBackend())
        build_time = time.perf_counter() - build_start
        rng = random.Random(seed)  # noqa: S311
        latencies = []
        n_queries = 10 if quick else 30
        for _ in range(n_queries):
            query = rng.choice(questions)
            start = time.perf_counter()
            fake_retriever.search(query, top_k=3)
            latencies.append(time.perf_counter() - start)
        p50, p95 = percentiles(latencies)
        latency_by_size[f"fake_backend_n={size}"] = (build_time, p50, p95)

    if not quick:
        real_questions = synthetic_catalog(1_000, seed)
        real_examples = build_catalog_examples(real_questions)
        build_start = time.perf_counter()
        real_retriever = CatalogRetriever(real_examples, LocalEmbeddingBackend())
        build_time = time.perf_counter() - build_start
        rng = random.Random(seed)  # noqa: S311
        latencies = [_time_search(real_retriever, rng.choice(real_questions)) for _ in range(15)]
        p50, p95 = percentiles(latencies)
        latency_by_size["real_backend_n=1000"] = (build_time, p50, p95)

    return Report(
        seed=seed,
        timestamp=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        catalog_size=len(catalog_questions),
        baseline=baseline,
        perturbation_results=perturbation_results,
        threshold_sweep=threshold_sweep,
        false_accept_rate=false_accept_rate,
        guard_bypasses=guard_bypasses,
        known_bypasses=known_bypasses,
        slot_fuzz_failures=slot_fuzz_failures,
        latency_by_size=latency_by_size,
    )


def _time_search(retriever: CatalogRetriever, query: str) -> float:
    start = time.perf_counter()
    retriever.search(query, top_k=3)
    return time.perf_counter() - start


def render_markdown(report: Report) -> str:
    lines = [
        "# RAG stress report",
        "",
        f"- model: `{MODEL_NAME}`",
        f"- catalog size: {report.catalog_size}",
        f"- seed: {report.seed}",
        f"- timestamp (UTC): {report.timestamp}",
        "",
        "## Baseline recall",
        "",
        f"recall@1={report.baseline[0]:.3f}  recall@3={report.baseline[1]:.3f}  "
        f"mean_top1_score={report.baseline[2]:.3f}",
        "",
        "## Perturbation robustness",
        "",
        "| class | recall@1 | Δ vs baseline | recall@3 | mean_score |",
        "|---|---|---|---|---|",
    ]
    for name, (r1, r3, mean_score) in report.perturbation_results.items():
        lines.append(
            f"| {name} | {r1:.3f} | {r1 - report.baseline[0]:+.3f} | {r3:.3f} | {mean_score:.3f} |"
        )

    lines += [
        "",
        "## Confidence threshold sweep",
        "",
        "| threshold | accept_rate (in-catalog, correct) | false_accept_rate (out-of-catalog) |",
        "|---|---|---|",
    ]
    for threshold, accept_rate, false_accept_rate in report.threshold_sweep:
        lines.append(f"| {threshold:.2f} | {accept_rate:.3f} | {false_accept_rate:.3f} |")

    lines += [
        "",
        f"False-accept rate at `CONFIDENCE_THRESHOLD=0.5`: {report.false_accept_rate:.3f}",
        "",
        "## Guard regressions (`ensure_read_only` mismatches vs pinned expectation)",
        "",
    ]
    if report.guard_bypasses:
        for case_id, sql, note in report.guard_bypasses:
            lines.append(f"- `{case_id}`: `{sql}` -- {note}")
    else:
        lines.append("None (no change vs pinned behaviour).")

    lines += [
        "",
        "## Known unfixed bypasses (guard WRONGLY allows these today)",
        "",
        "These are pinned as `expect_allow=True`, so they never count as a regression",
        "above. A zero regression count does NOT mean the guard is sound.",
        "",
    ]
    if report.known_bypasses:
        for case_id, sql, note in report.known_bypasses:
            lines.append(f"- **{case_id}**: `{sql}` -- {note}")
    else:
        lines.append("None.")

    lines += [
        "",
        f"## Slot-fuzz failures ({len(report.slot_fuzz_failures)})",
        "",
    ]
    if report.slot_fuzz_failures:
        for failure in report.slot_fuzz_failures[:20]:
            lines.append(f"- {failure}")
    else:
        lines.append("None (no change vs pinned behaviour).")

    lines += [
        "",
        "## Known unfixed bypasses (guard WRONGLY allows these today)",
        "",
        "These are pinned as `expect_allow=True`, so they never count as a regression",
        "above. A zero regression count does NOT mean the guard is sound.",
        "",
    ]
    if report.known_bypasses:
        for case_id, sql, note in report.known_bypasses:
            lines.append(f"- **{case_id}**: `{sql}` -- {note}")
    else:
        lines.append("None.")

    lines += [
        "",
        "## Latency by catalog size",
        "",
        "| config | build (s) | search p50 (ms) | search p95 (ms) |",
        "|---|---|---|---|",
    ]
    for label, (build_time, p50, p95) in report.latency_by_size.items():
        lines.append(f"| {label} | {build_time:.4f} | {p50 * 1000:.3f} | {p95 * 1000:.3f} |")

    lines.append("")
    return "\n".join(lines)


def write_csv(report: Report, path: Path) -> None:
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["section", "key", "metric", "value"])
        writer.writerow(["baseline", "-", "recall@1", report.baseline[0]])
        writer.writerow(["baseline", "-", "recall@3", report.baseline[1]])
        writer.writerow(["baseline", "-", "mean_top1_score", report.baseline[2]])
        for name, (r1, r3, mean_score) in report.perturbation_results.items():
            writer.writerow(["perturbation", name, "recall@1", r1])
            writer.writerow(["perturbation", name, "recall@3", r3])
            writer.writerow(["perturbation", name, "mean_score", mean_score])
        for threshold, accept_rate, false_accept_rate in report.threshold_sweep:
            writer.writerow(["threshold_sweep", threshold, "accept_rate", accept_rate])
            writer.writerow(["threshold_sweep", threshold, "false_accept_rate", false_accept_rate])
        writer.writerow(["false_accept", "default_0.5", "rate", report.false_accept_rate])
        writer.writerow(["guard_regression", "-", "count", len(report.guard_bypasses)])
        writer.writerow(["known_bypass", "-", "count", len(report.known_bypasses)])
        for case_id, sql, _note in report.known_bypasses:
            writer.writerow(["known_bypass", case_id, "sql", sql])
        writer.writerow(["slot_fuzz", "-", "failures", len(report.slot_fuzz_failures)])
        for label, (build_time, p50, p95) in report.latency_by_size.items():
            writer.writerow(["latency", label, "build_s", build_time])
            writer.writerow(["latency", label, "p50_ms", p50 * 1000])
            writer.writerow(["latency", label, "p95_ms", p95 * 1000])


@app.command()
def main(
    seed: Annotated[int, typer.Option(help="Seed for all RNGs used in the battery.")] = 20260829,
    quick: Annotated[
        bool, typer.Option(help="Small/fast sanity run instead of the full battery.")
    ] = False,
    out: Annotated[
        Path, typer.Option(help="Directory to write the report into.")
    ] = DEFAULT_REPORTS_DIR,
) -> None:
    """Run the RAG stress battery and write a timestamped markdown+CSV report."""
    report = run_battery(seed=seed, quick=quick)
    out.mkdir(parents=True, exist_ok=True)

    md_path = out / f"rag_stress_{report.timestamp}.md"
    csv_path = out / f"rag_stress_{report.timestamp}.csv"
    md_path.write_text(render_markdown(report))
    write_csv(report, csv_path)

    typer.echo(f"Wrote {md_path}")
    typer.echo(f"Wrote {csv_path}")


if __name__ == "__main__":
    app()
