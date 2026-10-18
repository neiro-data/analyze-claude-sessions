"""Run the real-model ask stress battery and write a timestamped report.

Reuses `analyze_claude_sessions.nlp_stress` (perturbations, `FakeHashBackend`,
`SQL_ADVERSARIAL_CORPUS`, `generate_slot_questions`) and the real
`CatalogRetriever` / `LocalEmbeddingBackend` from `analyze_claude_sessions.nlp`.
Mirrors the measurements in `tests/test_nlp_stress_slow.py`, packaged as a
standalone, runnable report generator rather than a test.

Run:

    uv run --group nlp python scripts/ask_stress.py            # full battery
    uv run --group nlp python scripts/ask_stress.py --quick    # small/fast sanity run
    uv run --group nlp python scripts/ask_stress.py -m BAAI/bge-small-en-v1.5   # one other model
    uv run --group nlp python scripts/ask_stress.py --compare  # MiniLM, bge-small, mpnet

`--model/-m` is repeatable; `--compare` is shorthand for all of `COMPARISON_MODELS`.
Run `make ask-stress-prefetch` once first so every model is cached (the comparison
refuses to start with an uncached model, see `_preflight`).

Writes `data/reports/ask_stress_<timestamp>.md` and `.csv` (CSV leads with a `model` column).
"""

from __future__ import annotations

import csv
import gc
import math
import os
import random
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from analyze_claude_sessions.nlp import (
    DEFAULT_MODEL_NAME,
    Backend,
    CatalogRetriever,
    LocalEmbeddingBackend,
    UnsafeQueryError,
    ensure_read_only,
    extract_slots,
)
from analyze_claude_sessions.nlp_stress import (
    COMPARISON_SWEEP,
    CONFIDENCE_SWEEP,
    PERTURBATIONS,
    SQL_ADVERSARIAL_CORPUS,
    FakeHashBackend,
    build_catalog_examples,
    generate_slot_questions,
    load_yaml,
    near_miss_cases,
    operating_point,
    paraphrase_pairs,
    percentiles,
    score_pairs,
    separation_auc,
    synthetic_catalog,
)

app = typer.Typer(add_completion=False)

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_PATH = REPO_ROOT / "tests" / "data" / "nlp_eval.yaml"
STRESS_PATH = REPO_ROOT / "tests" / "data" / "nlp_stress.yaml"
DEFAULT_REPORTS_DIR = REPO_ROOT / "data" / "reports"

# Models compared by `--compare`. Names are derived/explicit, never inferred from a run.
COMPARISON_MODELS: tuple[str, ...] = (
    DEFAULT_MODEL_NAME,
    "BAAI/bge-small-en-v1.5",
    "all-mpnet-base-v2",
)

#: Factory used by the CLI to build a backend from a model name. Tests swap it.
BACKEND_FACTORY: Callable[[str], Backend] = LocalEmbeddingBackend

NO_MODEL = "-"  # CSV `model` value for model-independent sections


@dataclass
class ModelReport:
    model_name: str
    load_s: float
    encode_p50_s: float
    encode_p95_s: float
    embed_dim: int
    disk_mb: float | None
    baseline: tuple[float, float, float]
    perturbation_results: dict[str, tuple[float, float, float]]
    threshold_sweep: list[tuple[float, float, float]]
    false_accept_rate: float  # out-of-catalog, at CONFIDENCE_THRESHOLD (0.5)
    n_ooc: int
    operating_threshold: float  # t*: lowest threshold with OOC false-accept <= 10%
    accept_at_op: float
    ooc_fa_at_op: float
    near_miss_rate_at_op: float
    near_miss_rate_default: float
    separation_auc: float
    # (question, kind, top-1 catalog question, score, top-1 is the decoy)
    near_miss_hits: list[tuple[str, str, str, float, bool]]
    real_latency: tuple[float, float, float] | None  # (build, p50, p95) at n=1000


@dataclass
class Report:
    seed: int
    timestamp: str
    catalog_size: int
    models: list[ModelReport]
    guard_bypasses: list[tuple[str, str, str]]
    known_bypasses: list[tuple[str, str, str]]
    slot_fuzz_failures: list[str]
    latency_by_size: dict[
        str, tuple[float, float, float]
    ]  # fake backend: label -> (build, p50, p95)


def _hf_cache_size_mb(model_name: str) -> float | None:
    """Size of the model's blobs in the local HF cache, or None if not cached.

    Mirrors `LocalEmbeddingBackend._is_cached` path rules without importing
    huggingface_hub (which would freeze HF_HUB_OFFLINE).
    """
    repo_id = model_name if "/" in model_name else f"sentence-transformers/{model_name}"
    hf_home = os.environ.get("HF_HOME")
    cache_root = (
        Path(hf_home) / "hub" if hf_home else Path.home() / ".cache" / "huggingface" / "hub"
    )
    blobs = cache_root / f"models--{repo_id.replace('/', '--')}" / "blobs"
    if not blobs.is_dir():
        return None
    return sum(f.stat().st_size for f in blobs.iterdir() if f.is_file()) / (1024 * 1024)


def _run_model(
    model_name: str,
    *,
    seed: int,
    quick: bool,
    eval_data: dict[str, Any],
    stress_data: dict[str, Any],
    backend_factory: Callable[[str], Backend] = LocalEmbeddingBackend,
    sweep: tuple[float, ...] = CONFIDENCE_SWEEP,
) -> ModelReport:
    catalog_questions = list(eval_data["catalog"].keys())
    examples = build_catalog_examples(catalog_questions)

    load_start = time.perf_counter()
    backend = backend_factory(model_name)
    warm = backend.encode(["warmup"])
    load_s = time.perf_counter() - load_start
    embed_dim = int(warm.shape[1])

    # No cache_dir on purpose: cache hits would skew the load/encode timings.
    retriever = CatalogRetriever(examples, backend)

    pairs = paraphrase_pairs(eval_data)

    encode_times: list[float] = []
    for i in range(10 if quick else 30):
        query = pairs[i % len(pairs)][1]
        start = time.perf_counter()
        backend.encode([query])
        encode_times.append(time.perf_counter() - start)
    encode_p50, encode_p95 = percentiles(encode_times)

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
    in_catalog_scores: list[tuple[float, bool]] = []
    for catalog_question, query in pairs:
        ranked = retriever.search(query, top_k=1)
        if ranked:
            in_catalog_scores.append((ranked[0][1], ranked[0][0].question == catalog_question))
    ooc_ranked = [retriever.search(q, top_k=1) for q in out_of_catalog]
    ooc_scores = [ranked[0][1] if ranked else -1.0 for ranked in ooc_ranked]

    threshold_sweep = []
    for threshold in sweep:
        accepted_correct = sum(
            1 for score, correct in in_catalog_scores if score >= threshold and correct
        )
        accept_rate = accepted_correct / len(in_catalog_scores)
        false_accepts = sum(1 for score in ooc_scores if score >= threshold)
        threshold_sweep.append((threshold, accept_rate, false_accepts / len(ooc_scores)))

    false_accept_rate = sum(1 for r in ooc_ranked if r and retriever.is_confident(r[0][1])) / len(
        out_of_catalog
    )

    near_miss_hits: list[tuple[str, str, str, float, bool]] = []
    for case in near_miss_cases(eval_data):
        ranked = retriever.search(case.question, top_k=1)
        if ranked:
            top, score = ranked[0]
            near_miss_hits.append(
                (case.question, case.kind, top.question, score, top.question == case.decoy)
            )
        else:
            near_miss_hits.append((case.question, case.kind, "-", -1.0, False))
    near_miss_scores = [hit[3] for hit in near_miss_hits]

    # t* is tuned on out-of-catalog rows only; near-miss rows are the held-out check.
    t_star = operating_point(in_catalog_scores, ooc_scores)
    n_in = len(in_catalog_scores)
    accept_at_op = sum(1 for sc, ok in in_catalog_scores if ok and sc >= t_star) / n_in
    ooc_fa_at_op = sum(1 for sc in ooc_scores if sc >= t_star) / len(ooc_scores)

    def _rate(scores: list[float], threshold: float) -> float:
        return sum(1 for sc in scores if sc >= threshold) / len(scores) if scores else 0.0

    positives = [sc for sc, ok in in_catalog_scores if ok]
    negatives = ooc_scores + near_miss_scores
    auc = separation_auc(positives, negatives) if positives and negatives else math.nan

    real_latency: tuple[float, float, float] | None = None
    if not quick:
        real_questions = synthetic_catalog(1_000, seed)
        build_start = time.perf_counter()
        real_retriever = CatalogRetriever(build_catalog_examples(real_questions), backend)
        build_time = time.perf_counter() - build_start
        rng = random.Random(seed)  # noqa: S311
        latencies = [_time_search(real_retriever, rng.choice(real_questions)) for _ in range(15)]
        p50, p95 = percentiles(latencies)
        real_latency = (build_time, p50, p95)
        del real_retriever

    report = ModelReport(
        model_name=model_name,
        load_s=load_s,
        encode_p50_s=encode_p50,
        encode_p95_s=encode_p95,
        embed_dim=embed_dim,
        disk_mb=_hf_cache_size_mb(model_name),
        baseline=baseline,
        perturbation_results=perturbation_results,
        threshold_sweep=threshold_sweep,
        false_accept_rate=false_accept_rate,
        n_ooc=len(ooc_scores),
        operating_threshold=t_star,
        accept_at_op=accept_at_op,
        ooc_fa_at_op=ooc_fa_at_op,
        near_miss_rate_at_op=_rate(near_miss_scores, t_star),
        near_miss_rate_default=sum(1 for sc in near_miss_scores if retriever.is_confident(sc))
        / len(near_miss_scores)
        if near_miss_scores
        else 0.0,
        separation_auc=auc,
        near_miss_hits=near_miss_hits,
        real_latency=real_latency,
    )
    # Release the model before the next one loads: peak memory is one model.
    del retriever, backend
    gc.collect()
    return report


def _run_shared(
    *, seed: int, quick: bool
) -> tuple[
    list[tuple[str, str, str]],
    list[tuple[str, str, str]],
    list[str],
    dict[str, tuple[float, float, float]],
]:
    """Model-independent sections: guard, slot fuzz, fake-backend latency."""
    guard_bypasses: list[tuple[str, str, str]] = []
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
    for case_ in generate_slot_questions(rng, count=slot_count):
        try:
            extract_slots(
                case_.question,
                known_projects=list(case_.known_projects),
                known_models=list(case_.known_models),
                today=case_.today,
            )
        except Exception as exc:  # noqa: BLE001 - fuzz target
            slot_fuzz_failures.append(f"{case_.question!r} raised {exc!r}")

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
    return guard_bypasses, known_bypasses, slot_fuzz_failures, latency_by_size


def run_battery(
    *,
    seed: int,
    quick: bool,
    models: list[str] | None = None,
    backend_factory: Callable[[str], Backend] = LocalEmbeddingBackend,
) -> Report:
    model_names = models or [DEFAULT_MODEL_NAME]
    eval_data = load_yaml(EVAL_PATH)
    stress_data = load_yaml(STRESS_PATH)
    sweep = COMPARISON_SWEEP if len(model_names) > 1 else CONFIDENCE_SWEEP

    model_reports = [
        _run_model(
            name,
            seed=seed,
            quick=quick,
            eval_data=eval_data,
            stress_data=stress_data,
            backend_factory=backend_factory,
            sweep=sweep,
        )
        for name in model_names
    ]
    guard_bypasses, known_bypasses, slot_fuzz_failures, latency_by_size = _run_shared(
        seed=seed, quick=quick
    )
    return Report(
        seed=seed,
        timestamp=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        catalog_size=len(eval_data["catalog"]),
        models=model_reports,
        guard_bypasses=guard_bypasses,
        known_bypasses=known_bypasses,
        slot_fuzz_failures=slot_fuzz_failures,
        latency_by_size=latency_by_size,
    )


def _time_search(retriever: CatalogRetriever, query: str) -> float:
    start = time.perf_counter()
    retriever.search(query, top_k=3)
    return time.perf_counter() - start


def _fmt_disk(disk_mb: float | None) -> str:
    return "n/a" if disk_mb is None else f"{disk_mb:.0f}"


def _comparison_table(models: list[ModelReport]) -> list[str]:
    lines = [
        "## Model comparison",
        "",
        "t* = lowest threshold with out-of-catalog false-accept <= 10% (set it as the model's",
        "`CONFIDENCE_THRESHOLD`); tuned on out-of-catalog rows only. accept@t* = correct top-1",
        "and score >= t*. AUC = correct in-catalog top-1 vs out-of-catalog + near-miss top-1.",
        "`@0.5` columns are at the current `CONFIDENCE_THRESHOLD` (MiniLM-tuned).",
        "",
        "| model | recall@1 | recall@3 | mean perturb recall@1 | worst class | t* (n_ooc) | "
        "accept@t* | AUC | OOC FA@0.5 | near-miss WA@t* | near-miss WA@0.5 | load s | "
        "encode p50/p95 ms | dim | disk MB |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for m in models:
        perturb = m.perturbation_results
        mean_pert = statistics.fmean(v[0] for v in perturb.values()) if perturb else math.nan
        worst = min(perturb.items(), key=lambda kv: kv[1][0]) if perturb else None
        worst_txt = f"{worst[0]} ({worst[1][0]:.3f})" if worst else "n/a"
        lines.append(
            f"| `{m.model_name}` | {m.baseline[0]:.3f} | {m.baseline[1]:.3f} | {mean_pert:.3f} | "
            f"{worst_txt} | {m.operating_threshold:.3f} ({m.n_ooc}) | {m.accept_at_op:.3f} | "
            f"{m.separation_auc:.3f} | {m.false_accept_rate:.3f} | {m.near_miss_rate_at_op:.3f} | "
            f"{m.near_miss_rate_default:.3f} | {m.load_s:.2f} | "
            f"{m.encode_p50_s * 1000:.1f}/{m.encode_p95_s * 1000:.1f} | {m.embed_dim} | "
            f"{_fmt_disk(m.disk_mb)} |"
        )
    return lines


def _near_miss_table(m: ModelReport) -> list[str]:
    lines = [
        "| question | kind | top-1 | score | accepted@t* | hit decoy? |",
        "|---|---|---|---|---|---|",
    ]
    for question, kind, top, score, is_decoy in m.near_miss_hits:
        accepted = "yes" if score >= m.operating_threshold else "no"
        lines.append(
            f"| {question} | {kind} | {top} | {score:.3f} | {accepted} | "
            f"{'yes' if is_decoy else 'no'} |"
        )
    return lines


def _model_sections(m: ModelReport, level: str, *, include_near_miss: bool) -> list[str]:
    sub = level + "#"  # heading prefix of this model's sections
    lines = [
        f"{sub} Baseline recall",
        "",
        f"recall@1={m.baseline[0]:.3f}  recall@3={m.baseline[1]:.3f}  "
        f"mean_top1_score={m.baseline[2]:.3f}",
        "",
        f"{sub} Perturbation robustness",
        "",
        "| class | recall@1 | Δ vs baseline | recall@3 | mean_score |",
        "|---|---|---|---|---|",
    ]
    for name, (r1, r3, mean_score) in m.perturbation_results.items():
        lines.append(
            f"| {name} | {r1:.3f} | {r1 - m.baseline[0]:+.3f} | {r3:.3f} | {mean_score:.3f} |"
        )

    lines += [
        "",
        f"{sub} Confidence threshold sweep",
        "",
        "| threshold | accept_rate (in-catalog, correct) | false_accept_rate (out-of-catalog) |",
        "|---|---|---|",
    ]
    for threshold, accept_rate, false_accept_rate in m.threshold_sweep:
        lines.append(f"| {threshold:.2f} | {accept_rate:.3f} | {false_accept_rate:.3f} |")

    lines += [
        "",
        "False-accept rate at `CONFIDENCE_THRESHOLD=0.5` "
        f"(current, MiniLM-tuned): {m.false_accept_rate:.3f}",
        "",
        f"{sub} Operating point and separation",
        "",
        f"- operating threshold t* (OOC false-accept <= 10%, n_ooc={m.n_ooc}): "
        f"{m.operating_threshold:.3f}",
        f"- accept rate at t*: {m.accept_at_op:.3f}",
        f"- out-of-catalog false-accept at t*: {m.ooc_fa_at_op:.3f}",
        f"- near-miss wrong-accept at t*: {m.near_miss_rate_at_op:.3f}",
        "- near-miss wrong-accept at current CONFIDENCE_THRESHOLD (MiniLM-tuned, 0.5): "
        f"{m.near_miss_rate_default:.3f}",
        f"- separation AUC: {m.separation_auc:.3f}",
        f"- load {m.load_s:.2f}s, encode p50/p95 "
        f"{m.encode_p50_s * 1000:.1f}/{m.encode_p95_s * 1000:.1f} ms, dim {m.embed_dim}, "
        f"disk {_fmt_disk(m.disk_mb)} MB",
    ]
    if include_near_miss:
        lines += ["", f"{sub} Near-miss questions", "", *_near_miss_table(m)]
    if m.real_latency is not None:
        build_time, p50, p95 = m.real_latency
        lines += [
            "",
            f"{sub} Real-backend latency (n=1000)",
            "",
            "| config | build (s) | search p50 (ms) | search p95 (ms) |",
            "|---|---|---|---|",
            f"| real_backend_n=1000 | {build_time:.4f} | {p50 * 1000:.3f} | {p95 * 1000:.3f} |",
        ]
    lines.append("")
    return lines


def render_markdown(report: Report) -> str:
    multi = len(report.models) > 1
    names = ", ".join(f"`{m.model_name}`" for m in report.models)
    lines = [
        "# ask stress report",
        "",
        f"- {'models' if multi else 'model'}: {names}",
        f"- catalog size: {report.catalog_size}",
        f"- seed: {report.seed}",
        f"- timestamp (UTC): {report.timestamp}",
        "",
    ]
    if multi:
        lines += _comparison_table(report.models)
        lines += ["", "## Near-miss questions", ""]
        for m in report.models:
            lines += [f"### `{m.model_name}`", "", *_near_miss_table(m), ""]
        for m in report.models:
            lines += [f"## Model: `{m.model_name}`", ""]
            lines += _model_sections(m, "##", include_near_miss=False)
    else:
        lines += _model_sections(report.models[0], "#", include_near_miss=True)

    lines += [
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
        "## Latency by catalog size (fake backend)",
        "",
        "| config | build (s) | search p50 (ms) | search p95 (ms) |",
        "|---|---|---|---|",
    ]
    for label, (build_time, p50, p95) in report.latency_by_size.items():
        lines.append(f"| {label} | {build_time:.4f} | {p50 * 1000:.3f} | {p95 * 1000:.3f} |")

    lines += [
        "",
        'Note: the known `extract_slots` "top 10" quirk does not affect this report; the harness',
        "calls `retriever.search` directly and never `extract_slots` on catalog, paraphrase or",
        "near-miss text. bge is run without its query instruction prefix (symmetric short-question",
        "retrieval; `CatalogRetriever` encodes both sides identically) -- a possible",
        "follow-up variant.",
        "",
    ]
    return "\n".join(lines)


def write_csv(report: Report, path: Path) -> None:
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["model", "section", "key", "metric", "value"])
        for m in report.models:
            name = m.model_name
            writer.writerow([name, "baseline", "-", "recall@1", m.baseline[0]])
            writer.writerow([name, "baseline", "-", "recall@3", m.baseline[1]])
            writer.writerow([name, "baseline", "-", "mean_top1_score", m.baseline[2]])
            for cls, (r1, r3, mean_score) in m.perturbation_results.items():
                writer.writerow([name, "perturbation", cls, "recall@1", r1])
                writer.writerow([name, "perturbation", cls, "recall@3", r3])
                writer.writerow([name, "perturbation", cls, "mean_score", mean_score])
            for threshold, accept_rate, false_accept_rate in m.threshold_sweep:
                writer.writerow([name, "threshold_sweep", threshold, "accept_rate", accept_rate])
                writer.writerow(
                    [name, "threshold_sweep", threshold, "false_accept_rate", false_accept_rate]
                )
            writer.writerow([name, "false_accept", "default_0.5", "rate", m.false_accept_rate])
            for metric, value in (
                ("threshold", m.operating_threshold),
                ("n_ooc", m.n_ooc),
                ("accept_rate", m.accept_at_op),
                ("ooc_false_accept", m.ooc_fa_at_op),
                ("near_miss_wrong_accept", m.near_miss_rate_at_op),
            ):
                writer.writerow([name, "operating_point", "t_star", metric, value])
            writer.writerow(
                [name, "near_miss", "default_0.5", "wrong_accept", m.near_miss_rate_default]
            )
            writer.writerow([name, "separation", "-", "auc", m.separation_auc])
            for question, kind, _top, score, is_decoy in m.near_miss_hits:
                writer.writerow([name, "near_miss", question, "score", score])
                writer.writerow([name, "near_miss", question, "kind", kind])
                writer.writerow([name, "near_miss", question, "hit_decoy", is_decoy])
            for metric, value in (
                ("load_s", m.load_s),
                ("encode_p50_ms", m.encode_p50_s * 1000),
                ("encode_p95_ms", m.encode_p95_s * 1000),
                ("embed_dim", m.embed_dim),
                ("disk_mb", "" if m.disk_mb is None else m.disk_mb),
            ):
                writer.writerow([name, "model_info", "-", metric, value])
            if m.real_latency is not None:
                build_time, p50, p95 = m.real_latency
                label = "real_backend_n=1000"
                writer.writerow([name, "latency", label, "build_s", build_time])
                writer.writerow([name, "latency", label, "p50_ms", p50 * 1000])
                writer.writerow([name, "latency", label, "p95_ms", p95 * 1000])
        writer.writerow([NO_MODEL, "guard_regression", "-", "count", len(report.guard_bypasses)])
        writer.writerow([NO_MODEL, "known_bypass", "-", "count", len(report.known_bypasses)])
        for case_id, sql, _note in report.known_bypasses:
            writer.writerow([NO_MODEL, "known_bypass", case_id, "sql", sql])
        writer.writerow([NO_MODEL, "slot_fuzz", "-", "failures", len(report.slot_fuzz_failures)])
        for label, (build_time, p50, p95) in report.latency_by_size.items():
            writer.writerow([NO_MODEL, "latency", label, "build_s", build_time])
            writer.writerow([NO_MODEL, "latency", label, "p50_ms", p50 * 1000])
            writer.writerow([NO_MODEL, "latency", label, "p95_ms", p95 * 1000])


def _resolve_models(models: list[str] | None, compare: bool) -> list[str]:
    """Order-preserving, de-duplicated model list (default: MiniLM only)."""
    requested = list(models or [])
    if compare:
        requested += COMPARISON_MODELS
    return list(dict.fromkeys(requested)) or [DEFAULT_MODEL_NAME]


def _preflight(model_names: list[str]) -> None:
    """Fail fast if a multi-model run would hit an uncached model.

    `LocalEmbeddingBackend._load` sets HF_HUB_OFFLINE=1 process-wide after the
    first cached load and huggingface_hub freezes it, so a later uncached model
    would fail mid-run. Skipped when the user set HF_HUB_OFFLINE themselves.
    """
    user_offline = os.environ.get("HF_HUB_OFFLINE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if len(model_names) < 2 or user_offline:
        return
    missing = [n for n in model_names if not LocalEmbeddingBackend(n)._is_cached()]
    if missing:
        typer.echo(
            f"Models not in the local HF cache: {', '.join(missing)}.\n"
            "Run `make ask-stress-prefetch` first (downloads each model in its own process).",
            err=True,
        )
        raise typer.Exit(code=2)


@app.command()
def main(
    seed: Annotated[int, typer.Option(help="Seed for all RNGs used in the battery.")] = 20260829,
    quick: Annotated[
        bool, typer.Option(help="Small/fast sanity run instead of the full battery.")
    ] = False,
    out: Annotated[
        Path, typer.Option(help="Directory to write the report into.")
    ] = DEFAULT_REPORTS_DIR,
    model: Annotated[
        list[str] | None,
        typer.Option("--model", "-m", help="Embedding model; repeatable. Default: MiniLM."),
    ] = None,
    compare: Annotated[
        bool, typer.Option(help="Compare all of COMPARISON_MODELS (same as repeating --model).")
    ] = False,
) -> None:
    """Run the ask stress battery and write a timestamped markdown+CSV report."""
    model_names = _resolve_models(model, compare)
    if BACKEND_FACTORY is LocalEmbeddingBackend:
        _preflight(model_names)
    report = run_battery(
        seed=seed, quick=quick, models=model_names, backend_factory=BACKEND_FACTORY
    )
    out.mkdir(parents=True, exist_ok=True)

    md_path = out / f"ask_stress_{report.timestamp}.md"
    csv_path = out / f"ask_stress_{report.timestamp}.csv"
    md_path.write_text(render_markdown(report))
    write_csv(report, csv_path)

    typer.echo(f"Wrote {md_path}")
    typer.echo(f"Wrote {csv_path}")


if __name__ == "__main__":
    app()
